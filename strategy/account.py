"""Continuous account sync — the Delta demo account is the source of truth.

The engine keeps its own book (positions, fills, P&L). That book is a MODEL of
the account, and any model drifts: a fill that arrives after a timeout, a manual
trade placed in the Delta UI, a liquidation, a settlement. Before this module
the only reconciliation ran inside the engine loop every 20 cycles, which meant
a STOPPED engine never synced at all — the dashboard could show a book that had
been wrong for hours.

This service polls the exchange on its own schedule, independent of whether the
engine is running, and publishes a snapshot everything else reads:

    balances     GET /v2/wallet/balances     what the account is actually worth
    positions    GET /v2/positions/margined  what the account actually holds
    open orders  GET /v2/orders              what could still fill

Threading follows the same rule as Executor.snapshots(): `refresh()` blocks and
must run in a worker thread; `snapshot()` is a lock-free read safe to call from
the asyncio event loop. Acquiring a lock across REST calls on the loop is what
froze the whole app in the 2026-07-21 audit (#2) — don't reintroduce it.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from . import config
from .delta_client import DeltaAuthError, DeltaError, client
from .journal import journal


def _f(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


class AccountSync:
    #: A snapshot older than this is reported as stale rather than shown as fact.
    STALE_AFTER = 60.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snap: dict = {
            "synced_at": 0.0, "ok": False, "stale": True,
            "balances": [], "positions": [], "open_orders": [],
            "equity_usd": None, "available_usd": None,
            "errors": {}, "syncs": 0, "failures": 0,
        }
        self.syncs = 0
        self.failures = 0

    # -- read (safe from the event loop) -------------------------------- #
    def snapshot(self) -> dict:
        s = dict(self._snap)
        age = time.time() - s.get("synced_at", 0.0)
        s["age_sec"] = round(age, 1)
        s["stale"] = age > self.STALE_AFTER or not s.get("ok")
        return s

    def positions_by_product(self) -> dict[int, int]:
        """{product_id: signed size} from the last successful sync."""
        out: dict[int, int] = {}
        for p in self._snap.get("positions") or []:
            pid = p.get("product_id") or (p.get("product") or {}).get("id")
            try:
                size = int(float(p.get("size") or 0))
            except (TypeError, ValueError):
                size = 0
            if pid and size:
                out[int(pid)] = out.get(int(pid), 0) + size
        return out

    # -- write (worker thread ONLY — blocking REST) --------------------- #
    def refresh(self) -> dict:
        errors: dict[str, str] = {}
        balances: list[dict] = []
        positions: list[dict] = []
        orders: list[dict] = []
        equity = available = None

        try:
            raw = client.balances() or []
            for b in raw:
                bal = _f(b.get("balance"))
                if not bal:
                    continue
                balances.append({
                    "asset": b.get("asset_symbol"),
                    "balance": bal,
                    # available_balance = balance - blocked margin. This, not
                    # `balance`, is what can actually back a new order.
                    "available": _f(b.get("available_balance")),
                })
            usd = [b for b in balances
                   if (b["asset"] or "").upper() in ("USD", "USDT", "USDC")]
            if usd:
                equity = sum(b["balance"] or 0 for b in usd)
                available = sum(b["available"] or 0 for b in usd)
        except DeltaAuthError as exc:
            errors["balances"] = f"auth: {exc}"
        except DeltaError as exc:
            errors["balances"] = str(exc)[:200]

        try:
            positions = [p for p in (client.positions() or [])
                         if _f(p.get("size"))]
        except DeltaError as exc:
            errors["positions"] = str(exc)[:200]

        try:
            orders = client.open_orders() or []
        except DeltaError as exc:
            errors["open_orders"] = str(exc)[:200]

        ok = not errors
        self.syncs += 1
        if not ok:
            self.failures += 1

        snap = {
            "synced_at": time.time(), "ok": ok, "stale": False,
            "balances": balances, "positions": positions, "open_orders": orders,
            "equity_usd": equity, "available_usd": available,
            "position_count": len(positions), "open_order_count": len(orders),
            "errors": errors, "syncs": self.syncs, "failures": self.failures,
        }
        with self._lock:
            prev = self._snap
            self._snap = snap

        # Journal only transitions, not every poll — an error every 15s would
        # bury the trade record it shares a file with.
        if ok != prev.get("ok"):
            journal.record("account_sync", {
                "state": "recovered" if ok else "failing",
                "errors": errors or None,
                "equity_usd": equity, "positions": len(positions),
                "open_orders": len(orders)})
        return snap


sync = AccountSync()

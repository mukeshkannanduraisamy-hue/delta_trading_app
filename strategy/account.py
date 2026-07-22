"""Continuous account sync — SIMULATED PAPER TRADING.

This module simulates a virtual account. It no longer polls Delta API.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from . import config
from .journal import journal


class AccountSync:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.virtual_balance = config.SESSION_EQUITY_BASE
        # Track used margin. (margin = premium = entry_price * contracts * contract_value)
        self.used_margin = 0.0

        self._snap: dict = {
            "synced_at": time.time(), "ok": True, "stale": False,
            "balances": [], "positions": [], "open_orders": [],
            "equity_usd": self.virtual_balance, "available_usd": self.virtual_balance,
            "errors": {}, "syncs": 0, "failures": 0,
        }
        self.syncs = 0
        self.failures = 0

    def snapshot(self) -> dict:
        with self._lock:
            s = dict(self._snap)
        s["age_sec"] = 0.0
        s["stale"] = False
        return s

    def positions_by_product(self) -> dict[int, int]:
        return {}

    def deduct_margin(self, amount: float):
        with self._lock:
            self.used_margin += amount
            self._update_snap()

    def add_margin_and_pnl(self, margin_amount: float, pnl: float):
        with self._lock:
            self.used_margin = max(0.0, self.used_margin - margin_amount)
            self.virtual_balance += pnl
            self._update_snap()

    def _update_snap(self):
        self._snap.update({
            "synced_at": time.time(),
            "equity_usd": self.virtual_balance,
            "available_usd": self.virtual_balance - self.used_margin,
            "balances": [{"asset": "USDT", "balance": self.virtual_balance, "available": self.virtual_balance - self.used_margin}],
        })

    def refresh(self) -> dict:
        # In paper mode, refresh just returns the latest simulated snapshot.
        self.syncs += 1
        with self._lock:
            self._snap["syncs"] = self.syncs
            return dict(self._snap)


sync = AccountSync()


"""Order execution + open-position management.

Two modes:
  * paper     — fills are simulated at the live premium; virtual P&L only.
  * live_demo — real market orders are sent to the testnet demo book.

Every position carries a premium-based target and stop derived from the signal;
`manage()` closes positions when target/stop/time/expiry conditions are met.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from . import config
from .base import Signal
from .delta_client import DeltaError, client
from .journal import journal
from .market_data import OptionQuote, OptionResolver


def _fees(price: float, contract_value: float, contracts: int, spot: float = 0.0) -> float:
    """Delta options taker fee: min(0.03% of notional, 3.5% of premium) + 18% GST.

    Notional = underlying spot x contract_value x contracts. When spot is
    unknown (0) we fall back to the premium-cap term, which is the conservative
    (larger) charge for the cheap options this engine trades.
    """
    premium = price * contract_value * contracts
    premium_cap = config.FEE_PREMIUM_CAP * premium
    if spot > 0:
        notional_fee = config.FEE_NOTIONAL_RATE * spot * contract_value * contracts
        fee = min(notional_fee, premium_cap)
    else:
        fee = premium_cap
    return fee * (1 + config.GST_RATE)


@dataclass
class Position:
    id: str
    strategy: str
    direction: str          # CE | PE
    symbol: str
    product_id: int
    strike: float
    expiry: str
    entry_price: float
    contracts: int
    contract_value: float
    target: float
    stop: float
    mode: str
    reason: str
    opened_at: float
    max_hold_bars: Optional[int] = None
    bar_seconds: int = 60
    entry_fee: float = 0.0
    meta: dict = field(default_factory=dict)

    def as_dict(self, current_price: Optional[float] = None) -> dict:
        upnl = None
        if current_price is not None:
            upnl = (current_price - self.entry_price) * self.contract_value * self.contracts
        return {
            "id": self.id,
            "strategy": self.strategy,
            "direction": self.direction,
            "symbol": self.symbol,
            "strike": self.strike,
            "expiry": self.expiry,
            "entry_price": self.entry_price,
            "contracts": self.contracts,
            "target": self.target,
            "stop": self.stop,
            "current_price": current_price,
            "unrealized_pnl": upnl,
            "reason": self.reason,
            "opened_at": self.opened_at,
            "age_sec": time.time() - self.opened_at,
            "mode": self.mode,
        }


class Executor:
    def __init__(self, resolver: OptionResolver) -> None:
        self.resolver = resolver
        self.positions: dict[str, Position] = {}
        self.balance = config.PAPER_START_BALANCE
        self.realized_pnl = 0.0
        self.wins = 0
        self.losses = 0
        self.closed_count = 0

    # ------------------------------------------------------------------ #
    def has_open(self, strategy: str, direction: str) -> bool:
        return any(
            p.strategy == strategy and p.direction == direction
            for p in self.positions.values()
        )

    def open_count(self) -> int:
        return len(self.positions)

    # ------------------------------------------------------------------ #
    def open(self, signal: Signal, quote: OptionQuote, bar_seconds: int) -> Optional[dict]:
        if self.open_count() >= config.MAX_OPEN_POSITIONS:
            return None
        if self.has_open(signal.strategy, signal.direction):
            return None

        entry = quote.best_ask or quote.mark_price
        if not entry or entry <= 0:
            journal.record("skip", {"strategy": signal.strategy, "direction": signal.direction,
                                    "reason": "no tradable premium", "symbol": quote.symbol})
            return None

        # Live demo: place a real market buy on the testnet book.
        if config.LIVE_DEMO:
            try:
                res = client.place_order(quote.product_id, config.CONTRACTS, "buy", "market_order")
                fill = res.get("average_fill_price") or res.get("limit_price")
                if fill:
                    entry = float(fill)
            except DeltaError as exc:
                journal.record("order_error", {"strategy": signal.strategy, "symbol": quote.symbol,
                                               "error": str(exc)})
                return None

        contracts = config.CONTRACTS
        fee = _fees(entry, quote.contract_value, contracts)
        pos = Position(
            id=uuid.uuid4().hex[:8],
            strategy=signal.strategy,
            direction=signal.direction,
            symbol=quote.symbol,
            product_id=quote.product_id,
            strike=quote.strike,
            expiry=quote.expiry,
            entry_price=entry,
            contracts=contracts,
            contract_value=quote.contract_value,
            target=entry * (1 + signal.target_pct),
            stop=entry * (1 - signal.sl_pct),
            mode=config.EXECUTION_MODE,
            reason=signal.reason,
            opened_at=time.time(),
            max_hold_bars=signal.max_hold_bars or config.DEFAULT_MAX_HOLD_BARS,
            bar_seconds=bar_seconds,
            entry_fee=fee,
            meta=signal.meta,
        )
        self.positions[pos.id] = pos
        self.balance -= fee
        journal.record("open", {
            "id": pos.id, "strategy": pos.strategy, "direction": pos.direction,
            "symbol": pos.symbol, "strike": pos.strike, "entry_price": entry,
            "target": pos.target, "stop": pos.stop, "contracts": contracts,
            "reason": pos.reason, "mode": pos.mode,
        })
        return pos.as_dict(entry)

    # ------------------------------------------------------------------ #
    def _close(self, pos: Position, price: float, why: str) -> None:
        gross = (price - pos.entry_price) * pos.contract_value * pos.contracts
        exit_fee = _fees(price, pos.contract_value, pos.contracts)
        # entry_fee was already removed from balance at open; the full round-trip
        # net (for realized P&L / win-loss) charges both legs.
        net = gross - pos.entry_fee - exit_fee
        self.balance += gross - exit_fee
        self.realized_pnl += net
        self.closed_count += 1
        if net >= 0:
            self.wins += 1
        else:
            self.losses += 1
        if config.LIVE_DEMO:
            try:
                client.place_order(pos.product_id, pos.contracts, "sell", "market_order")
            except DeltaError as exc:
                journal.record("order_error", {"id": pos.id, "symbol": pos.symbol,
                                               "error": f"exit: {exc}"})
        journal.record("close", {
            "id": pos.id, "strategy": pos.strategy, "direction": pos.direction,
            "symbol": pos.symbol, "entry_price": pos.entry_price, "exit_price": price,
            "gross_pnl": gross, "net_pnl": net, "why": why, "mode": pos.mode,
        })
        self.positions.pop(pos.id, None)

    def manage(self) -> list[dict]:
        """Poll each open position's premium and close on target/stop/time."""
        snapshots: list[dict] = []
        for pos in list(self.positions.values()):
            price = self.resolver.live_price(pos.symbol)
            if price is None:
                snapshots.append(pos.as_dict(None))
                continue
            if price >= pos.target:
                self._close(pos, price, "target")
                continue
            if price <= pos.stop:
                self._close(pos, price, "stop")
                continue
            age_bars = (time.time() - pos.opened_at) / max(pos.bar_seconds, 1)
            if pos.max_hold_bars and age_bars >= pos.max_hold_bars:
                self._close(pos, price, "time_exit")
                continue
            snapshots.append(pos.as_dict(price))
        return snapshots

    def flatten_all(self, why: str = "manual_flatten") -> int:
        n = 0
        for pos in list(self.positions.values()):
            price = self.resolver.live_price(pos.symbol) or pos.entry_price
            self._close(pos, price, why)
            n += 1
        return n

    # ------------------------------------------------------------------ #
    def stats(self) -> dict:
        total = self.wins + self.losses
        return {
            "balance": round(self.balance, 4),
            "realized_pnl": round(self.realized_pnl, 4),
            "open_positions": self.open_count(),
            "closed_trades": self.closed_count,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.wins / total * 100, 1) if total else None,
        }

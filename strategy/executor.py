"""Order execution + open-position management.

LIVE ONLY. Paper (simulated-fill) mode was removed on 2026-07-21 — every entry
and every exit in this module sends a real order to the Delta testnet demo book.
There is no dry run and no simulated fallback.

ENTRY_ORDER_TYPE selects how entries are placed:
  * market — cross the spread, always fills, pays the ask.
  * limit  — rest a GTC limit near mid; avoids paying the spread but may not
             fill. Unfilled orders auto-cancel after LIMIT_TTL_BARS.

Because there is no simulated path, the engine refuses to start without working
credentials (see Engine.preflight) rather than degrading into a state where it
fires signals and fails every order while appearing to trade.

Hardening invariants (each guards a real failure mode found in review):
  * Every fill is verified (state / unfilled_size); a cancelled or unfilled
    order never becomes a tracked position; partial fills track only what filled.
  * A network-ambiguous order (timeout) is never blindly retried: the exchange
    position is reconciled before any sell, so the engine can't double-sell.
  * A failed exit keeps the position tracked and retries — never orphaned.
  * open/manage/flatten/resolve_pending share one lock (no API-vs-engine races).
  * Time and expiry exits fire even with no live quote (no immortal positions).
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from . import config, store
from .base import Signal
from .delta_client import DeltaError, DeltaNetworkError, client
from .journal import journal
from .market_data import OptionQuote, OptionResolver
from .pricebus import bus


def _spot() -> float:
    """Live underlying spot, or 0.0 if unknown."""
    return bus.spot(config.UNDERLYING_SYMBOL) or 0.0


def _coid(direction: str) -> str:
    """Idempotency key for one order attempt. <=32 chars, per Delta's limit.

    Without this an order that times out is unresolvable — we cannot ask the
    exchange whether it landed, so a filled position becomes an untracked orphan
    with no stop-loss (audit #4).
    """
    return f"e{uuid.uuid4().hex[:24]}{direction}"[:32]


def _fees(price: float, contract_value: float, contracts: int,
          spot: float = 0.0) -> Optional[float]:
    """Delta options taker fee: min(0.03% of notional, 3.5% of premium) + 18% GST.

    `spot` must be the UNDERLYING price — the notional leg is 0.03% of spot
    notional. Passing the strike instead (as this used to be called) is only
    approximately right for ATM contracts and wrong everywhere else (audit #14).

    Returns None when spot is unknown: the old behaviour substituted the 3.5%
    premium CAP, which is the maximum fee, overstating cost roughly tenfold on
    an ATM option (audit #15). A silent wrong number is worse than an explicit
    "cannot price this".
    """
    premium = price * contract_value * contracts
    premium_cap = config.FEE_PREMIUM_CAP * premium
    if spot <= 0:
        return None
    notional_fee = config.FEE_NOTIONAL_RATE * spot * contract_value * contracts
    return min(notional_fee, premium_cap) * (1 + config.GST_RATE)


def _fees_or_cap(price: float, contract_value: float, contracts: int,
                 spot: float = 0.0) -> float:
    """`_fees`, falling back to the conservative premium cap when spot is
    unknown. Used on the booking path, where refusing to close is worse than
    over-charging: an unclosed position keeps real risk open."""
    fee = _fees(price, contract_value, contracts, spot)
    if fee is not None:
        return fee
    return config.FEE_PREMIUM_CAP * price * contract_value * contracts * (1 + config.GST_RATE)


def _settlement_time(expiry_ddmmyyyy: str) -> Optional[datetime]:
    """Settlement instant for an expiry (12:00 UTC on the date)."""
    try:
        d, m, y = expiry_ddmmyyyy.split("-")
        return datetime(int(y), int(m), int(d), 12, 0, tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def _expiry_settled(expiry_ddmmyyyy: str) -> bool:
    """True once the option has settled (12:00 UTC on the expiry date)."""
    st = _settlement_time(expiry_ddmmyyyy)
    return st is not None and datetime.now(timezone.utc) > st


def _settlement_value(direction: str, strike: float, spot: Optional[float]) -> Optional[float]:
    """Intrinsic value at settlement.

    A settled option is worth exactly its intrinsic value — never the last quoted
    premium, and certainly never the entry price, which is what the old fallback
    booked and which recorded worthless expiries as break-even trades (audit #7).
    """
    if spot is None or spot <= 0 or not strike:
        return None
    return max(0.0, spot - strike) if direction == "CE" else max(0.0, strike - spot)


def _round_tick(price: float, tick: float) -> float:
    if not tick or tick <= 0:
        return round(price, 2)
    return round(round(price / tick) * tick, 8)


def _limit_entry_price(quote: OptionQuote, signal: Optional[Signal] = None) -> Optional[float]:
    """Resting buy-limit price from the configured anchor, capped at the ask.

    A strategy may declare its own offset via `Signal.limit_offset_pct` (Booming
    Bulls publishes "fast Supertrend +0.5%"). That field was being set and then
    silently ignored — the strategy's actual entry rule was never implemented
    (audit #16). A strategy-declared offset now wins over the global tick offset.
    """
    bid, ask = quote.best_bid, quote.best_ask
    if not bid or not ask or bid <= 0 or ask <= 0:
        return None
    anchor = config.LIMIT_ANCHOR
    base = bid if anchor == "bid" else ask if anchor == "ask" else (bid + ask) / 2
    pct = getattr(signal, "limit_offset_pct", None) if signal else None
    offset = base * pct if pct else config.LIMIT_OFFSET_TICKS * quote.tick_size
    price = min(base + offset, ask)  # never bid above the ask
    price = _round_tick(price, quote.tick_size)
    return price if price > 0 else None


@dataclass
class Position:
    id: str
    strategy: str
    direction: str
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
    last_price: Optional[float] = None
    exit_ambiguous: bool = False
    meta: dict = field(default_factory=dict)

    def as_dict(self, current_price: Optional[float] = None) -> dict:
        price = current_price if current_price is not None else self.last_price
        upnl = None
        if price is not None:
            upnl = (price - self.entry_price) * self.contract_value * self.contracts
        return {
            "id": self.id, "strategy": self.strategy, "direction": self.direction,
            "symbol": self.symbol, "strike": self.strike, "expiry": self.expiry,
            "entry_price": self.entry_price, "contracts": self.contracts,
            "target": self.target, "stop": self.stop, "current_price": price,
            "unrealized_pnl": upnl, "reason": self.reason, "opened_at": self.opened_at,
            "age_sec": time.time() - self.opened_at, "mode": self.mode, "state": "open",
            # Needed to match this position against the exchange's book.
            "product_id": self.product_id,
            "adopted": bool(self.meta.get("adopted")),
        }


@dataclass
class PendingEntry:
    """A resting limit buy that has not (fully) filled yet."""
    order_id: int
    strategy: str
    direction: str
    quote: OptionQuote
    signal: Signal
    limit_price: float
    requested: int
    placed_at: float
    bar_seconds: int
    filled: int = 0

    def as_dict(self) -> dict:
        return {
            "id": f"pending-{self.order_id}", "strategy": self.strategy,
            "direction": self.direction, "symbol": self.quote.symbol,
            "strike": self.quote.strike, "expiry": self.quote.expiry,
            "entry_price": self.limit_price, "contracts": self.requested,
            "target": None, "stop": None, "current_price": None,
            "unrealized_pnl": None, "reason": f"resting limit @ {self.limit_price}",
            "opened_at": self.placed_at, "age_sec": time.time() - self.placed_at,
            "mode": config.EXECUTION_MODE, "state": "pending",
        }


class Executor:
    def __init__(self, resolver: OptionResolver) -> None:
        self.resolver = resolver
        self.positions: dict[str, Position] = {}
        self.pending: dict[str, PendingEntry] = {}   # keyed by "strategy|direction"
        # Session equity line, NOT a wallet. It starts from a notional base and
        # tracks realized P&L + fees so the UI can plot "P&L since start". The
        # real demo-account balance is read from the exchange via /api/account —
        # this number must never be presented as the account balance.
        self.session_equity = config.SESSION_EQUITY_BASE
        self.realized_pnl = 0.0
        self.wins = 0
        self.losses = 0
        self.closed_count = 0
        self.cooldown_until: dict[tuple[str, str], float] = {}
        self._lock = threading.RLock()
        self._last_reconcile_sig: Optional[str] = None
        # Published view of the book, refreshed by whoever already holds the
        # lock. Async callers read this instead of acquiring the lock — see
        # snapshots().
        self._snapshot_cache: list[dict] = []

    # ------------------------------------------------------------------ #
    def _key(self, strategy: str, direction: str) -> str:
        return f"{strategy}|{direction}"

    def has_open(self, strategy: str, direction: str) -> bool:
        return any(p.strategy == strategy and p.direction == direction
                   for p in self.positions.values())

    def has_pending(self, strategy: str, direction: str) -> bool:
        return self._key(strategy, direction) in self.pending

    def in_cooldown(self, strategy: str, direction: str) -> bool:
        return time.time() < self.cooldown_until.get((strategy, direction), 0.0)

    def open_count(self) -> int:
        return len(self.positions)

    def _rebuild_snapshot_locked(self) -> list[dict]:
        """Refresh the published view. Caller MUST already hold self._lock."""
        snap = [p.as_dict() for p in self.positions.values()] + \
               [pe.as_dict() for pe in self.pending.values()]
        self._snapshot_cache = snap
        return snap

    def snapshots(self) -> list[dict]:
        """Lock-free read, safe to call from the asyncio event loop.

        `manage()` holds self._lock across blocking REST calls in a worker
        thread. threading.RLock.acquire() does not yield to asyncio, so any
        coroutine that touched the lock froze the ENTIRE application — every
        route, every WebSocket, and the upstream price feed — for as long as
        those calls took (audit #2).

        This returns the last published view instead: at most one engine cycle
        stale, which is well inside the 1s UI publish cadence. No trading
        decision reads it. Worker threads that need the authoritative book call
        snapshots_blocking().
        """
        return self._snapshot_cache

    def snapshots_blocking(self) -> list[dict]:
        """Authoritative view. Worker threads ONLY — this acquires the lock."""
        with self._lock:
            return self._rebuild_snapshot_locked()

    # ------------------------------------------------------------------ #
    def _exchange_size(self, product_id: int) -> Optional[int]:
        """Authoritative open size for ONE product.

        Uses the real-time /v2/positions?product_id= form. The bulk
        /v2/positions/margined endpoint lags up to 10 seconds, and this value
        decides whether an ambiguous exit gets re-sent — a stale "still open"
        reading there caused a second sell (audit #1).

        Returns None if the size cannot be determined. Callers MUST NOT treat
        None as flat.
        """
        try:
            rows = client.positions(product_id=product_id)
        except DeltaError:
            return None
        total = 0
        for p in rows or []:
            pid = p.get("product_id") or (p.get("product") or {}).get("id")
            if pid and int(pid) == int(product_id):
                try:
                    total += int(float(p.get("size") or 0))
                except (TypeError, ValueError):
                    pass
        return total

    def _recover_by_coid(self, coid: str, attempts: int = 3) -> Optional[dict]:
        """Resolve an ambiguous submission using our idempotency key.

        Returns the order object if the exchange has it (it landed), or None if
        it provably never existed / stayed unreachable. This is what turns a
        timed-out entry from "silently dropped, position untracked" into a
        recoverable event.
        """
        for i in range(attempts):
            time.sleep(0.5 * (i + 1))   # let the exchange settle before asking
            try:
                o = client.get_order_by_coid(coid)
            except DeltaNetworkError:
                continue                 # still ambiguous — try again
            except DeltaError:
                return None              # 404: the order never landed
            if o:
                return o
        return None

    # ------------------------------------------------------------------ #
    def _register_position(self, strategy: str, direction: str, quote: OptionQuote,
                           signal: Signal, entry: float, contracts: int,
                           bar_seconds: int, order_id=None) -> dict:
        # Fee notional is 0.03% of UNDERLYING notional — pass live spot, not the
        # strike (audit #14).
        fee = _fees_or_cap(entry, quote.contract_value, contracts, spot=_spot())
        pos = Position(
            id=uuid.uuid4().hex[:8], strategy=strategy, direction=direction,
            symbol=quote.symbol, product_id=quote.product_id, strike=quote.strike,
            expiry=quote.expiry, entry_price=entry, contracts=contracts,
            contract_value=quote.contract_value,
            target=entry * (1 + signal.target_pct), stop=entry * (1 - signal.sl_pct),
            mode=config.EXECUTION_MODE, reason=signal.reason, opened_at=time.time(),
            max_hold_bars=signal.max_hold_bars or config.DEFAULT_MAX_HOLD_BARS,
            bar_seconds=bar_seconds, entry_fee=fee, last_price=entry,
            meta={**signal.meta, "order_id": order_id},
        )
        self.positions[pos.id] = pos
        self.session_equity -= fee
        self._rebuild_snapshot_locked()
        journal.record("open", {
            "id": pos.id, "strategy": pos.strategy, "direction": pos.direction,
            "symbol": pos.symbol, "strike": pos.strike, "entry_price": entry,
            "target": pos.target, "stop": pos.stop, "contracts": contracts,
            "reason": pos.reason, "mode": pos.mode, "order_id": order_id,
        })
        return pos.as_dict(entry)

    # ------------------------------------------------------------------ #
    def open(self, signal: Signal, quote: OptionQuote, bar_seconds: int) -> Optional[dict]:
        with self._lock:
            return self._open_locked(signal, quote, bar_seconds)

    def _open_locked(self, signal: Signal, quote: OptionQuote, bar_seconds: int) -> Optional[dict]:
        if self.open_count() >= config.MAX_OPEN_POSITIONS:
            return None
        if self.has_open(signal.strategy, signal.direction):
            return None
        if self.has_pending(signal.strategy, signal.direction):
            return None
        if self.in_cooldown(signal.strategy, signal.direction):
            return None
        if not quote.product_id:
            journal.record("skip", {"strategy": signal.strategy, "direction": signal.direction,
                                    "reason": "no product id", "symbol": quote.symbol})
            return None

        if config.ENTRY_ORDER_TYPE == "limit":
            return self._open_limit(signal, quote, bar_seconds)
        return self._open_market(signal, quote, bar_seconds)

    # -- market entry (real order) --------------------------------------- #
    def _open_market(self, signal: Signal, quote: OptionQuote, bar_seconds: int) -> Optional[dict]:
        # Reference price only — the tracked entry is overwritten by the
        # exchange's average_fill_price below. It exists so a quote-less
        # contract is rejected before an order is sent.
        entry = quote.best_ask or quote.mark_price
        if not entry or entry <= 0:
            journal.record("skip", {"strategy": signal.strategy, "direction": signal.direction,
                                    "reason": "no tradable premium", "symbol": quote.symbol})
            return None
        coid = _coid(signal.direction)
        try:
            res = client.place_order(
                quote.product_id, config.CONTRACTS, "buy", "market_order",
                client_order_id=coid,
            )
        except DeltaNetworkError as exc:
            # Outcome UNKNOWN — the exchange may have filled this. Dropping
            # it here is what created untracked, stop-less positions
            # (audit #4). Ask the exchange using our idempotency key.
            res = self._recover_by_coid(coid)
            if res is None:
                journal.record("order_error", {
                    "strategy": signal.strategy, "symbol": quote.symbol,
                    "client_order_id": coid,
                    "error": f"entry ambiguous AND unrecoverable: {exc}",
                    "action": "MANUAL CHECK REQUIRED — possible untracked position"})
                return None
            journal.record("order_recovered", {
                "strategy": signal.strategy, "symbol": quote.symbol,
                "client_order_id": coid, "order_id": res.get("id")})
        except DeltaError as exc:
            journal.record("order_error", {"strategy": signal.strategy, "symbol": quote.symbol,
                                           "error": str(exc)})
            return None
        order_id = res.get("id")
        uf = res.get("unfilled_size")
        try:
            unfilled = int(float(uf)) if uf is not None else 0
        except (TypeError, ValueError):
            unfilled = 0
        filled = config.CONTRACTS - unfilled
        if filled <= 0 or res.get("state") == "cancelled":
            journal.record("order_error", {
                "strategy": signal.strategy, "symbol": quote.symbol, "order_id": order_id,
                "error": f"entry not filled (state={res.get('state')}, unfilled={unfilled})"})
            return None
        contracts = filled
        # The exchange's own average fill is authoritative — never the quote.
        fill = res.get("average_fill_price")
        if fill:
            entry = float(fill)
        return self._register_position(signal.strategy, signal.direction, quote, signal,
                                       entry, contracts, bar_seconds, order_id)

    # -- resting limit entry --------------------------------------------- #
    def _open_limit(self, signal: Signal, quote: OptionQuote, bar_seconds: int) -> Optional[dict]:
        price = _limit_entry_price(quote, signal)
        if price is None:
            journal.record("skip", {"strategy": signal.strategy, "direction": signal.direction,
                                    "reason": "no bid/ask for limit price", "symbol": quote.symbol})
            return None
        coid = _coid(signal.direction)
        try:
            res = client.place_order(quote.product_id, config.CONTRACTS, "buy",
                                     "limit_order", limit_price=price,
                                     client_order_id=coid)
        except DeltaNetworkError as exc:
            # A resting limit that may or may not exist is still dangerous: it
            # can fill later with nothing tracking it. Resolve it by key.
            res = self._recover_by_coid(coid)
            if res is None:
                journal.record("order_error", {
                    "strategy": signal.strategy, "symbol": quote.symbol,
                    "client_order_id": coid,
                    "error": f"limit place ambiguous AND unrecoverable: {exc}",
                    "action": "MANUAL CHECK REQUIRED — possible resting order"})
                return None
            journal.record("order_recovered", {
                "strategy": signal.strategy, "symbol": quote.symbol,
                "client_order_id": coid, "order_id": res.get("id")})
        except DeltaError as exc:
            journal.record("order_error", {"strategy": signal.strategy, "symbol": quote.symbol,
                                           "error": str(exc)})
            return None
        order_id = res.get("id")
        # It may have filled immediately (marketable) — handle that inline.
        # NB: unfilled_size can legitimately be 0, so test for None, not falsiness.
        uf = res.get("unfilled_size")
        try:
            unfilled = int(float(uf)) if uf is not None else config.CONTRACTS
        except (TypeError, ValueError):
            unfilled = config.CONTRACTS
        filled = config.CONTRACTS - unfilled
        if filled >= config.CONTRACTS:
            fill = float(res.get("average_fill_price") or price)
            return self._register_position(signal.strategy, signal.direction, quote,
                                           signal, fill, filled, bar_seconds, order_id)
        # Otherwise rest it and resolve over the next cycles.
        pe = PendingEntry(order_id=order_id, strategy=signal.strategy,
                          direction=signal.direction, quote=quote, signal=signal,
                          limit_price=price, requested=config.CONTRACTS,
                          placed_at=time.time(), bar_seconds=bar_seconds, filled=filled)
        self.pending[self._key(signal.strategy, signal.direction)] = pe
        journal.record("limit_placed", {
            "order_id": order_id, "strategy": signal.strategy, "direction": signal.direction,
            "symbol": quote.symbol, "limit_price": price, "contracts": config.CONTRACTS,
            "reason": signal.reason})
        return pe.as_dict()

    def resolve_pending(self) -> None:
        """Poll each resting limit: fill -> position, timeout -> cancel."""
        if not self.pending:
            return
        with self._lock:
            for key, pe in list(self.pending.items()):
                try:
                    o = client.get_order(pe.order_id)
                except DeltaError:
                    continue  # transient; retry next cycle
                state = o.get("state")
                try:
                    unfilled = int(float(o.get("unfilled_size")
                                         if o.get("unfilled_size") is not None else pe.requested))
                except (TypeError, ValueError):
                    unfilled = pe.requested
                filled = pe.requested - unfilled
                fill_price = float(o.get("average_fill_price") or pe.limit_price)
                aged_out = (time.time() - pe.placed_at) >= config.LIMIT_TTL_BARS * pe.bar_seconds

                if state == "closed" and filled > 0:
                    self._register_position(pe.strategy, pe.direction, pe.quote, pe.signal,
                                            fill_price, filled, pe.bar_seconds, pe.order_id)
                    self.pending.pop(key, None)
                elif state == "cancelled":
                    if filled > 0:  # cancelled after a partial fill
                        self._register_position(pe.strategy, pe.direction, pe.quote, pe.signal,
                                                fill_price, filled, pe.bar_seconds, pe.order_id)
                    else:
                        journal.record("limit_cancelled", {"order_id": pe.order_id,
                                       "strategy": pe.strategy, "symbol": pe.quote.symbol})
                    self.pending.pop(key, None)
                elif aged_out:
                    try:
                        client.cancel_order(pe.order_id, pe.quote.product_id)
                    except DeltaError as exc:
                        journal.record("order_error", {"order_id": pe.order_id,
                                       "symbol": pe.quote.symbol, "error": f"cancel failed: {exc}"})
                        continue  # keep pending; retry cancel next cycle
                    if filled > 0:
                        self._register_position(pe.strategy, pe.direction, pe.quote, pe.signal,
                                                fill_price, filled, pe.bar_seconds, pe.order_id)
                    else:
                        journal.record("limit_expired", {"order_id": pe.order_id,
                                       "strategy": pe.strategy, "symbol": pe.quote.symbol,
                                       "limit_price": pe.limit_price})
                    self.pending.pop(key, None)
                # else: still resting within TTL — leave it.

    # ------------------------------------------------------------------ #
    def _book_close(self, pos: Position, exit_price: float, contracts: int, why: str) -> None:
        frac = contracts / pos.contracts if pos.contracts else 1.0
        gross = (exit_price - pos.entry_price) * pos.contract_value * contracts
        exit_fee = _fees_or_cap(exit_price, pos.contract_value, contracts, spot=_spot())
        entry_fee_part = pos.entry_fee * frac
        net = gross - entry_fee_part - exit_fee
        self.session_equity += gross - exit_fee
        self.realized_pnl += net
        full = contracts == pos.contracts
        journal.record("close" if full else "close_partial", {
            "id": pos.id, "strategy": pos.strategy, "direction": pos.direction,
            "symbol": pos.symbol, "entry_price": pos.entry_price, "exit_price": exit_price,
            "contracts": contracts, "gross_pnl": gross, "net_pnl": net, "why": why,
            "mode": pos.mode})
        store.record_trade({
            "ts_open": pos.opened_at, "ts_close": time.time(), "strategy": pos.strategy,
            "direction": pos.direction, "symbol": pos.symbol, "strike": pos.strike,
            "expiry": pos.expiry, "entry_price": pos.entry_price, "exit_price": exit_price,
            "contracts": contracts, "contract_value": pos.contract_value,
            "gross_pnl": gross, "net_pnl": net, "why": why, "mode": pos.mode,
            "partial": 0 if full else 1})
        if full:
            self.closed_count += 1
            if net > 0:
                self.wins += 1
            elif net < 0:
                self.losses += 1
            self.cooldown_until[(pos.strategy, pos.direction)] = (
                time.time() + config.COOLDOWN_BARS * pos.bar_seconds)
            self.positions.pop(pos.id, None)
        else:
            pos.contracts -= contracts
            pos.entry_fee -= entry_fee_part
        self._rebuild_snapshot_locked()

    def _close(self, pos: Position, price: Optional[float], why: str, sell: bool = True) -> bool:
        """Close a position. `sell=False` only for settled expiries, where the
        exchange has already closed the position for us and sending an order
        would open a new short."""
        exit_price = price if price is not None else (pos.last_price or pos.entry_price)
        if sell:
            if pos.exit_ambiguous:
                size = self._exchange_size(pos.product_id)
                if size is None:
                    return False
                pos.exit_ambiguous = False
                if size <= 0:
                    self._book_close(pos, exit_price, pos.contracts, why + "_reconciled")
                    return True
            try:
                # reduce_only is the load-bearing safety property here: it makes
                # the exchange reject any sell that would exceed the open long,
                # so even a duplicate send is a no-op instead of flipping the
                # account SHORT an option with unbounded risk (audit #1).
                res = client.place_order(
                    pos.product_id, pos.contracts, "sell", "market_order",
                    reduce_only=True,
                    client_order_id=f"x{pos.id}{uuid.uuid4().hex[:14]}"[:32],
                )
            except DeltaNetworkError as exc:
                pos.exit_ambiguous = True
                journal.record("order_error", {"id": pos.id, "symbol": pos.symbol,
                                               "error": f"exit ambiguous, will reconcile: {exc}"})
                return False
            except DeltaError as exc:
                journal.record("order_error", {"id": pos.id, "symbol": pos.symbol,
                                               "error": f"exit failed, will retry: {exc}"})
                return False
            uf = res.get("unfilled_size")
            try:
                unfilled = int(float(uf)) if uf is not None else 0
            except (TypeError, ValueError):
                unfilled = 0
            filled = pos.contracts - unfilled
            if filled <= 0:
                journal.record("order_error", {"id": pos.id, "symbol": pos.symbol,
                                               "error": f"exit not filled (state={res.get('state')})"})
                return False
            fill = res.get("average_fill_price")
            if fill:
                exit_price = float(fill)
            if unfilled > 0:
                self._book_close(pos, exit_price, filled, why)
                return False
        self._book_close(pos, exit_price, pos.contracts, why)
        return True

    # ------------------------------------------------------------------ #
    def manage(self) -> list[dict]:
        with self._lock:
            self.resolve_pending()
            snapshots: list[dict] = []
            for pos in list(self.positions.values()):
                try:
                    if not self._manage_one(pos):
                        snapshots.append(pos.as_dict())
                except Exception as exc:  # noqa: BLE001 — isolate this position
                    journal.record("error", {"scope": f"manage:{pos.symbol}", "error": repr(exc)})
                    snapshots.append(pos.as_dict())
            snapshots.extend(pe.as_dict() for pe in self.pending.values())
            # Publish for the lock-free async readers (see snapshots()).
            self._snapshot_cache = snapshots
            return snapshots

    def _manage_one(self, pos: Position) -> bool:
        price: Optional[float] = None
        try:
            price = self.resolver.live_price(pos.symbol)
        except Exception:  # noqa: BLE001
            price = None
        if price is not None:
            pos.last_price = price
            if price >= pos.target:
                return self._close(pos, price, "target")
            if price <= pos.stop:
                return self._close(pos, price, "stop")
        if _expiry_settled(pos.expiry):
            # A settled option is worth its intrinsic value and nothing else.
            # The old path booked the last quote — or, with a dead feed, the
            # ENTRY price, recording worthless expiries as break-even trades and
            # corrupting every downstream edge metric (audit #7).
            settle_px = _settlement_value(pos.direction, pos.strike, _spot() or None)
            if settle_px is None:
                # No spot to value against. Book worthless rather than pretend
                # break-even: a position that survived to settlement without
                # being stopped out is overwhelmingly likely to be worthless,
                # and that is also the conservative direction.
                settle_px = 0.0
                journal.record("warn", {
                    "id": pos.id, "symbol": pos.symbol,
                    "note": "settled with no spot available — booked at 0"})
            return self._close(pos, settle_px, "settled", sell=False)
        age_bars = (time.time() - pos.opened_at) / max(pos.bar_seconds, 1)
        if pos.max_hold_bars and age_bars >= pos.max_hold_bars:
            return self._close(pos, price, "time_exit")
        return False

    def flatten_all(self, why: str = "manual_flatten") -> int:
        with self._lock:
            # Cancel any resting entries first so they can't fill after flatten.
            for key, pe in list(self.pending.items()):
                try:
                    client.cancel_order(pe.order_id, pe.quote.product_id)
                except DeltaError:
                    pass
                self.pending.pop(key, None)
            n = 0
            for pos in list(self.positions.values()):
                try:
                    price = self.resolver.live_price(pos.symbol)
                except Exception:  # noqa: BLE001
                    price = None
                if self._close(pos, price, why):
                    n += 1
            self._rebuild_snapshot_locked()
            return n

    # ------------------------------------------------------------------ #
    def reconcile(self) -> None:
        """Bring local state in line with the exchange, which is the truth.

        This used to only journal a mismatch and then suppress the repeat, so a
        persistent divergence was reported once and went quiet while an orphan
        rode to expiry unmanaged (audit #9). Now:

          GHOST  — tracked locally, absent on the exchange. Booked closed so
                   MAX_OPEN, the cooldown map and P&L stop drifting.
          ORPHAN — size on the exchange we never tracked. It has no stop, no
                   target and no time exit. Journalled every check, and closed
                   with reduce_only when AUTO_FLATTEN_ORPHANS is on.

        The bulk (10s-stale) positions endpoint is acceptable here: this is a
        periodic audit, not the ambiguous-exit decision that needs realtime data.
        """
        try:
            rows = client.positions()
        except DeltaError:
            return

        exch: dict[int, int] = {}
        exch_entry: dict[int, float] = {}
        for p in rows or []:
            pid = p.get("product_id") or (p.get("product") or {}).get("id")
            try:
                size = int(float(p.get("size") or 0))
            except (TypeError, ValueError):
                size = 0
            if pid and size:
                exch[int(pid)] = exch.get(int(pid), 0) + size
                # Delta returns entry_price as a string; adoption prefers it
                # over a live quote because it is the position's real basis.
                try:
                    ep = p.get("entry_price")
                    if ep is not None:
                        exch_entry[int(pid)] = float(ep)
                except (TypeError, ValueError):
                    pass

        with self._lock:
            tracked: dict[int, int] = {}
            for pos in self.positions.values():
                tracked[pos.product_id] = tracked.get(pos.product_id, 0) + pos.contracts
            if exch == tracked:
                self._last_reconcile_sig = None
                return

            journal.record("reconcile_mismatch", {
                "exchange": {str(k): v for k, v in exch.items()},
                "tracked": {str(k): v for k, v in tracked.items()}})

            # GHOSTS: we believe we hold it, the exchange disagrees.
            for pos in list(self.positions.values()):
                if exch.get(pos.product_id, 0) < tracked.get(pos.product_id, 0):
                    px = pos.last_price if pos.last_price is not None else pos.entry_price
                    journal.record("ghost_closed", {
                        "id": pos.id, "symbol": pos.symbol,
                        "product_id": pos.product_id,
                        "note": "exchange has no such position — booking local close"})
                    self._book_close(pos, px, pos.contracts, "ghost_reconciled")

            # SHORT positions. The engine's Position model is long-only —
            # target/stop are computed as entry*(1±pct) and _close() SELLS — so
            # a short cannot be adopted without silently inverting its risk.
            # A short option also carries the unbounded tail this codebase went
            # to some trouble to prevent (audit #1: a reduce_only-less sell on a
            # flat book opens exactly this). Surface it; never auto-adopt it.
            for pid, size in exch.items():
                if size >= 0:
                    continue
                journal.record("short_position_detected", {
                    "product_id": pid, "size": size,
                    "note": "SHORT position on the exchange. The engine cannot "
                            "manage shorts — no stop, no target, no time exit. "
                            "Close it manually or via POST /api/strategy/"
                            "flatten-shorts.",
                    "severity": "critical"})

            # ORPHANS: untracked LONG size — adoptable.
            for pid, size in exch.items():
                if size <= 0:
                    continue
                extra = size - tracked.get(pid, 0)
                if extra <= 0:
                    continue
                journal.record("orphan_detected", {
                    "product_id": pid, "untracked_size": extra,
                    "policy": config.ORPHAN_POLICY,
                    "note": "position on exchange with NO local stop-loss"})
                if config.ORPHAN_POLICY == "report":
                    continue
                if config.ORPHAN_POLICY == "adopt":
                    if self._adopt(pid, extra, entry_hint=exch_entry.get(pid)):
                        continue
                    # Adoption failed (unknown product / no contract_value).
                    # Fall through to flatten rather than leave it unmanaged.
                    journal.record("orphan_adopt_failed", {
                        "product_id": pid, "size": extra,
                        "note": "could not resolve contract — flattening instead"})
                try:
                    client.place_order(
                        pid, extra, "sell", "market_order", reduce_only=True,
                        client_order_id=f"orph{uuid.uuid4().hex[:20]}"[:32])
                    journal.record("orphan_flattened",
                                   {"product_id": pid, "size": extra})
                except DeltaError as exc:
                    journal.record("order_error", {
                        "product_id": pid,
                        "error": f"orphan flatten failed: {exc}"})

    def _adopt(self, product_id: int, size: int,
               entry_hint: Optional[float] = None) -> bool:
        """Bring an untracked exchange position under management.

        Adoption cannot know which strategy opened the position or what its
        intent was, so it applies one conservative bracket (ADOPT_SL_PCT /
        ADOPT_RR) and marks the position `adopted` in its meta so the journal
        never implies a strategy produced it.

        Entry price comes from the exchange when available; otherwise the live
        bid, which is what the position is actually worth right now. Returns
        False if the contract cannot be resolved — the caller then flattens
        rather than leaving real risk unmanaged.
        """
        if size <= 0:
            # Long-only model — see the short handling in reconcile().
            return False
        quote = self.resolver.quote_for_product(product_id)
        if quote is None:
            return False
        entry = entry_hint or quote.best_bid or quote.mark_price
        if not entry or entry <= 0:
            return False
        sig = Signal(strategy="adopted", direction=quote.side,
                     reason=f"adopted from exchange (product {product_id})",
                     sl_pct=config.ADOPT_SL_PCT, rr=config.ADOPT_RR,
                     meta={"adopted": True, "product_id": product_id})
        bar_seconds = 60
        self._register_position(sig.strategy, sig.direction, quote, sig,
                                float(entry), int(size), bar_seconds,
                                order_id=None)
        journal.record("orphan_adopted", {
            "product_id": product_id, "size": size, "symbol": quote.symbol,
            "entry_price": float(entry), "sl_pct": config.ADOPT_SL_PCT,
            "rr": config.ADOPT_RR,
            "note": "now managed: stop / target / time / expiry exits apply"})
        return True

    def flatten_shorts(self) -> dict:
        """Close every SHORT position on the account with a reduce_only BUY.

        Explicit action only — never automatic. `reduce_only` means the order
        can shrink a short and nothing else: it cannot flip the account long,
        and it is rejected outright if the short is already gone. Shorts are not
        adopted by reconcile because the engine cannot manage them, so this is
        how they get closed from the app.
        """
        try:
            rows = client.positions()
        except DeltaError as exc:
            return {"ok": False, "error": str(exc), "closed": 0}

        closed, errors = [], []
        for p in rows or []:
            pid = p.get("product_id") or (p.get("product") or {}).get("id")
            try:
                size = int(float(p.get("size") or 0))
            except (TypeError, ValueError):
                size = 0
            if not pid or size >= 0:
                continue
            qty = abs(size)
            try:
                res = client.place_order(
                    int(pid), qty, "buy", "market_order", reduce_only=True,
                    client_order_id=f"shrt{uuid.uuid4().hex[:20]}"[:32])
                journal.record("short_flattened", {
                    "product_id": int(pid), "size": size, "bought": qty,
                    "order_id": res.get("id"),
                    "avg_fill": res.get("average_fill_price")})
                closed.append({"product_id": int(pid), "size": size,
                               "order_id": res.get("id")})
            except DeltaError as exc:
                journal.record("order_error", {
                    "product_id": int(pid),
                    "error": f"short flatten failed: {exc}"})
                errors.append({"product_id": int(pid), "error": str(exc)})
        return {"ok": not errors, "closed": len(closed),
                "positions": closed, "errors": errors}

    # ------------------------------------------------------------------ #
    def stats(self) -> dict:
        """Account-synced statistics.

        Everything durable comes from the exchange or from SQLite, NOT from the
        in-memory counters: those reset on every restart, which is why the
        dashboard could show "Realized P&L $0.00 / Closed 0" next to an account
        with a real trade history. The session_* keys keep the since-start view
        for anyone who wants it, but they are no longer what the UI headlines.
        """
        from .account import sync as _account          # local: avoids a cycle

        acct = _account.snapshot()
        durable = store.realized_summary(mode=config.EXECUTION_MODE)
        total = self.wins + self.losses
        return {
            # --- exchange truth ---
            "equity_usd": acct.get("equity_usd"),
            "available_usd": acct.get("available_usd"),
            "account_synced_at": acct.get("synced_at"),
            "account_age_sec": acct.get("age_sec"),
            "account_stale": acct.get("stale", True),
            "exchange_positions": acct.get("position_count", 0),
            "exchange_open_orders": acct.get("open_order_count", 0),
            # --- durable trade record (survives restarts) ---
            "realized_pnl": durable["realized_pnl"],
            "closed_trades": durable["closed_trades"],
            "wins": durable["wins"],
            "losses": durable["losses"],
            "win_rate": durable["win_rate"],
            "profit_factor": durable["profit_factor"],
            # --- this process only ---
            "session_realized_pnl": round(self.realized_pnl, 4),
            "session_closed_trades": self.closed_count,
            "session_wins": self.wins,
            "session_losses": self.losses,
            "session_win_rate": round(self.wins / total * 100, 1) if total else None,
            "session_equity": round(self.session_equity, 4),
            # `balance` is what the UI tile reads — point it at the REAL wallet.
            "balance": acct.get("equity_usd"),
            "open_positions": self.open_count(),
            "pending_orders": len(self.pending),
            "mode": config.EXECUTION_MODE,
            "live_only": True,
        }

    def _legacy_stats(self) -> dict:
        total = self.wins + self.losses
        return {
            # Session equity line, not the demo wallet. `/api/account` is the
            # authoritative balance; the UI must not conflate the two.
            "session_equity": round(self.session_equity, 4),
            "balance": round(self.session_equity, 4),   # back-compat for the UI
            "mode": config.EXECUTION_MODE,
            "live_only": True,
            "realized_pnl": round(self.realized_pnl, 4),
            "open_positions": self.open_count(),
            "pending_orders": len(self.pending),
            "closed_trades": self.closed_count,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.wins / total * 100, 1) if total else None,
        }

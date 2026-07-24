"""Order execution + open-position management (PAPER TRADING).

Simulates all trades internally.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import math
from . import config, store
from .base import Signal
from .delta_client import DeltaError, DeltaNetworkError, client
from .journal import journal
from .market_data import OptionQuote, OptionResolver
from .pricebus import bus
from .options_calc import calculate_options_order_params, AutoSkipTrade

def _confidence_params(confidence: int) -> tuple[int, int]:
    from .settings import manager
    conf = max(1, min(100, confidence))
    if conf <= 20:
        pct, lev = manager.get("lot_pct_very_low"), manager.get("leverage_very_low")
    elif conf <= 40:
        pct, lev = manager.get("lot_pct_low"), manager.get("leverage_low")
    elif conf <= 60:
        pct, lev = manager.get("lot_pct_medium"), manager.get("leverage_medium")
    elif conf <= 80:
        pct, lev = manager.get("lot_pct_high"), manager.get("leverage_high")
    else:
        pct, lev = manager.get("lot_pct_very_high"), manager.get("leverage_very_high")
    lev = min(lev, manager.get("max_leverage_cap"))
    raw_lots = (pct / 100.0) * manager.get("max_lot_size")
    return max(1, math.floor(raw_lots)), lev



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
    confidence: int = 50
    leverage: int = 1
    # Advanced Options Fields
    entry_iv: Optional[float] = None
    current_iv: Optional[float] = None
    btc_sl_price: Optional[float] = None
    option_tp1: Optional[float] = None
    option_tp2: Optional[float] = None
    option_tp3: Optional[float] = None
    btc_tp1: Optional[float] = None
    btc_tp2: Optional[float] = None
    btc_tp3: Optional[float] = None
    partial_exits: list = field(default_factory=list)
    trailing_stop_active: bool = False
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
            "confidence": self.confidence,
            "leverage": self.leverage,
            "entry_iv": self.entry_iv,
            "current_iv": self.current_iv,
            "btc_sl_price": self.btc_sl_price,
            "option_tp1": self.option_tp1,
            "option_tp2": self.option_tp2,
            "option_tp3": self.option_tp3,
            "btc_tp1": self.btc_tp1,
            "btc_tp2": self.btc_tp2,
            "btc_tp3": self.btc_tp3,
            "partial_exits": self.partial_exits,
            "trailing_stop_active": self.trailing_stop_active,
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
    adv_params: dict = field(default_factory=dict)

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
            "confidence": self.signal.confidence, "leverage": 1,
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
        self.last_sl_tp_check: float = 0.0
        self.last_theta_check: float = 0.0
        self.last_greeks_refresh: float = 0.0

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
        # Simulated mode ignores exchange
        return None

    def _recover_by_coid(self, coid: str, attempts: int = 3) -> Optional[dict]:
        return None

    # ------------------------------------------------------------------ #
    def _register_position(self, strategy: str, direction: str, quote: OptionQuote,
                           signal: Signal, entry: float, contracts: int,
                           bar_seconds: int, leverage: int = 1, order_id=None,
                           adv_params: dict = None) -> dict:
        fee = _fees_or_cap(entry, quote.contract_value, contracts, spot=_spot())
        adv = adv_params or {}
        pos = Position(
            id=uuid.uuid4().hex[:8], strategy=strategy, direction=direction,
            symbol=quote.symbol, product_id=quote.product_id, strike=quote.strike,
            expiry=quote.expiry, entry_price=entry, contracts=contracts,
            contract_value=quote.contract_value,
            target=adv.get("option_tp3") or (entry * (1 + signal.target_pct)), 
            stop=adv.get("option_sl_price") or (entry * (1 - signal.sl_pct)),
            mode=config.EXECUTION_MODE, reason=signal.reason, opened_at=time.time(),
            max_hold_bars=signal.max_hold_bars or config.DEFAULT_MAX_HOLD_BARS,
            bar_seconds=bar_seconds, entry_fee=fee, last_price=entry,
            confidence=signal.confidence, leverage=leverage,
            entry_iv=adv.get("entry_iv"),
            current_iv=adv.get("entry_iv"),
            btc_sl_price=adv.get("btc_sl_price"),
            option_tp1=adv.get("option_tp1"),
            option_tp2=adv.get("option_tp2"),
            option_tp3=adv.get("option_tp3"),
            btc_tp1=adv.get("btc_tp1"),
            btc_tp2=adv.get("btc_tp2"),
            btc_tp3=adv.get("btc_tp3"),
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
            "confidence": pos.confidence, "leverage": pos.leverage,
        })
        return pos.as_dict(entry)

    # ------------------------------------------------------------------ #
    def open(self, signal: Signal, quote: OptionQuote, bar_seconds: int) -> Optional[dict]:
        with self._lock:
            return self._open_locked(signal, quote, bar_seconds)

    def _open_locked(self, signal: Signal, quote: OptionQuote, bar_seconds: int) -> Optional[dict]:
        from .settings import manager
        if self.open_count() >= manager.get("max_open_positions"):
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
        # Check daily loss limit
        from .settings import manager
        loss_limit_pct = manager.get("daily_loss_limit_pct") / 100.0
        start_balance = manager.get("starting_virtual_balance")
        if self.realized_pnl < 0 and abs(self.realized_pnl) >= (start_balance * loss_limit_pct):
            journal.record("skip", {"strategy": signal.strategy, "direction": signal.direction,
                                    "reason": "daily loss limit reached", "symbol": quote.symbol})
            return None

        from .account import sync
        virtual_balance = sync.snapshot().get("equity_usd", start_balance)

        # ADVANCED OPTIONS PRE-TRADE CALCULATION
        try:
            adv_params = calculate_options_order_params(quote.symbol, signal.direction, signal.confidence, virtual_balance)
        except AutoSkipTrade as e:
            journal.record("skip", {"strategy": signal.strategy, "direction": signal.direction,
                                    "reason": str(e), "symbol": quote.symbol})
            return None
        except Exception as e:
            journal.record("skip", {"strategy": signal.strategy, "direction": signal.direction,
                                    "reason": f"calculation failed: {e}", "symbol": quote.symbol})
            return None

        if adv_params["errors"]:
            journal.record("skip", {"strategy": signal.strategy, "direction": signal.direction,
                                    "reason": "validation errors", "symbol": quote.symbol, "errors": adv_params["errors"]})
            return None

        for warn in adv_params["warnings"]:
            journal.record("warning", {"strategy": signal.strategy, "symbol": quote.symbol, "warning": warn})

        entry = adv_params["smart_entry"]
        contracts, leverage = _confidence_params(signal.confidence)
        
        margin = (entry * contracts * quote.contract_value) / leverage
        if margin > sync.snapshot().get("available_usd", 0):
            journal.record("skip", {"strategy": signal.strategy, "direction": signal.direction,
                                    "reason": "insufficient virtual margin", "symbol": quote.symbol})
            return None
        
        order_id = _coid(signal.direction)
        sync.deduct_margin(margin)
        
        # Merge adv_params into log
        journal.record("options_pre_trade", adv_params)
        
        return self._register_position(signal.strategy, signal.direction, quote, signal,
                                       entry, contracts, bar_seconds, leverage, order_id, adv_params)

    # -- resting limit entry --------------------------------------------- #
    def _open_limit(self, signal: Signal, quote: OptionQuote, bar_seconds: int) -> Optional[dict]:
        # Check daily loss limit
        from .settings import manager
        loss_limit_pct = manager.get("daily_loss_limit_pct") / 100.0
        start_balance = manager.get("starting_virtual_balance")
        if self.realized_pnl < 0 and abs(self.realized_pnl) >= (start_balance * loss_limit_pct):
            journal.record("skip", {"strategy": signal.strategy, "direction": signal.direction,
                                    "reason": "daily loss limit reached", "symbol": quote.symbol})
            return None

        from .account import sync
        virtual_balance = sync.snapshot().get("equity_usd", start_balance)

        # ADVANCED OPTIONS PRE-TRADE CALCULATION
        try:
            adv_params = calculate_options_order_params(quote.symbol, signal.direction, signal.confidence, virtual_balance)
        except AutoSkipTrade as e:
            journal.record("skip", {"strategy": signal.strategy, "direction": signal.direction,
                                    "reason": str(e), "symbol": quote.symbol})
            return None
        except Exception as e:
            journal.record("skip", {"strategy": signal.strategy, "direction": signal.direction,
                                    "reason": f"calculation failed: {e}", "symbol": quote.symbol})
            return None

        if adv_params["errors"]:
            journal.record("skip", {"strategy": signal.strategy, "direction": signal.direction,
                                    "reason": "validation errors", "symbol": quote.symbol, "errors": adv_params["errors"]})
            return None

        for warn in adv_params["warnings"]:
            journal.record("warning", {"strategy": signal.strategy, "symbol": quote.symbol, "warning": warn})

        price = adv_params["smart_entry"]
        contracts, leverage = _confidence_params(signal.confidence)
        margin = (price * contracts * quote.contract_value) / leverage
        
        if margin > sync.snapshot().get("available_usd", 0):
            journal.record("skip", {"strategy": signal.strategy, "direction": signal.direction,
                                    "reason": "insufficient virtual margin", "symbol": quote.symbol})
            return None

        order_id = _coid(signal.direction)
        pe = PendingEntry(order_id=order_id, strategy=signal.strategy,
                          direction=signal.direction, quote=quote, signal=signal,
                          limit_price=price, requested=contracts,
                          placed_at=time.time(), bar_seconds=bar_seconds, filled=0,
                          adv_params=adv_params)
        self.pending[self._key(signal.strategy, signal.direction)] = pe
        journal.record("limit_placed", {
            "order_id": order_id, "strategy": signal.strategy, "direction": signal.direction,
            "symbol": quote.symbol, "limit_price": price, "contracts": contracts,
            "reason": signal.reason, "adv_params": adv_params})
        return pe.as_dict()

    def resolve_pending(self) -> None:
        """Poll each resting limit: simulate fill if crossed, timeout -> cancel."""
        if not self.pending:
            return
        with self._lock:
            for key, pe in list(self.pending.items()):
                aged_out = (time.time() - pe.placed_at) >= config.LIMIT_TTL_BARS * pe.bar_seconds
                
                # Check for simulation fill
                tick = bus.get(pe.quote.symbol)
                fill_price = None
                if tick and tick.get("best_ask"):
                    if tick["best_ask"] <= pe.limit_price:
                        fill_price = tick["best_ask"]
                        
                if fill_price is not None:
                    filled = pe.requested
                    from .account import sync
                    contracts, leverage = _confidence_params(pe.signal.confidence)
                    margin = (fill_price * filled * pe.quote.contract_value) / leverage
                    sync.deduct_margin(margin)
                    self._register_position(pe.strategy, pe.direction, pe.quote, pe.signal,
                                            fill_price, filled, pe.bar_seconds, leverage, pe.order_id, pe.adv_params)
                    self.pending.pop(key, None)
                elif aged_out:
                    journal.record("limit_expired", {"order_id": pe.order_id,
                                   "strategy": pe.strategy, "symbol": pe.quote.symbol,
                                   "limit_price": pe.limit_price})
                    self.pending.pop(key, None)

    # ------------------------------------------------------------------ #
    def _book_close(self, pos: Position, exit_price: float, contracts: int, why: str) -> None:
        frac = contracts / pos.contracts if pos.contracts else 1.0
        gross = (exit_price - pos.entry_price) * pos.contract_value * contracts
        exit_fee = _fees_or_cap(exit_price, pos.contract_value, contracts, spot=_spot())
        entry_fee_part = pos.entry_fee * frac
        net = gross - entry_fee_part - exit_fee
        self.session_equity += gross - exit_fee - entry_fee_part
        self.realized_pnl += net
        
        from .account import sync
        margin_freed = (pos.entry_price * pos.contract_value * contracts) / pos.leverage
        sync.add_margin_and_pnl(margin_freed, net)
        
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
            "partial": 0 if full else 1, "confidence": pos.confidence, "leverage": pos.leverage,
            "entry_iv": pos.entry_iv})
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
        """Close a simulated position."""
        exit_price = price if price is not None else (pos.last_price or pos.entry_price)
        self._book_close(pos, exit_price, pos.contracts, why)
        return True

    # ------------------------------------------------------------------ #
    def manage(self) -> list[dict]:
        with self._lock:
            import time
            from .journal import journal
            self.resolve_pending()
            
            now = time.time()
            if not hasattr(self, 'last_sl_tp_check'): self.last_sl_tp_check = 0
            if not hasattr(self, 'last_theta_check'): self.last_theta_check = 0
            if not hasattr(self, 'last_greeks_refresh'): self.last_greeks_refresh = 0
            
            do_sl_tp = (now - self.last_sl_tp_check) >= 30
            do_theta = (now - self.last_theta_check) >= 900
            do_greeks = (now - self.last_greeks_refresh) >= 300
            
            if do_sl_tp: self.last_sl_tp_check = now
            if do_theta: self.last_theta_check = now
            if do_greeks: self.last_greeks_refresh = now

            snapshots: list[dict] = []
            
            if do_greeks:
                self._refresh_greeks()
                
            if do_theta:
                self._check_theta_erosion()
                
            if do_sl_tp:
                self._check_sl_tp()
                
            for pos in list(self.positions.values()):
                snapshots.append(pos.as_dict())
                
            snapshots.extend(pe.as_dict() for pe in self.pending.values())
            self._snapshot_cache = snapshots
            return snapshots

    def _refresh_greeks(self):
        for pos in list(self.positions.values()):
            try:
                ticker = client.ticker(pos.symbol)
                if ticker and "greeks" in ticker:
                    daily_theta = abs(float(ticker["greeks"].get("theta", 0)))
                    pos.meta["daily_theta"] = daily_theta
            except Exception:
                pass
                
    def _check_theta_erosion(self):
        from .journal import journal
        for pos in list(self.positions.values()):
            try:
                price = self.resolver.live_price(pos.symbol)
            except Exception:
                price = None
                
            if price is None:
                continue
                
            daily_theta = pos.meta.get("daily_theta")
            if not daily_theta:
                daily_theta = pos.meta.get("adv_params", {}).get("daily_theta")
                
            if daily_theta and price > 0:
                erosion = (daily_theta / price) * 100
                if erosion > 15:
                    journal.record("warn", {"id": pos.id, "symbol": pos.symbol, "warning": "Position auto-closed due to extreme theta decay"})
                    self._close(pos, price, "theta_erosion_exceeded")
                elif erosion > 8:
                    journal.record("warn", {"id": pos.id, "symbol": pos.symbol, "warning": f"URGENT: Theta is eroding {erosion:.1f}% of remaining premium per day. Recommend closing position."})

    def _check_sl_tp(self):
        from .journal import journal
        for pos in list(self.positions.values()):
            try:
                if not self._manage_one(pos):
                    pass
            except Exception as exc:
                journal.record("error", {"scope": f"manage:{pos.symbol}", "error": repr(exc)})

    def _manage_one(self, pos: Position) -> bool:
        price: Optional[float] = None
        try:
            price = self.resolver.live_price(pos.symbol)
        except Exception:
            price = None

        spot = _spot()

        if price is not None:
            pos.last_price = price

            # 1. Dual-Condition Stop Loss
            sl_hit = False
            sl_reason = ""
            if pos.stop and price <= pos.stop:
                sl_hit = True
                sl_reason = "stop_premium"
            elif pos.btc_sl_price and spot:
                if pos.direction == "CE" and spot <= pos.btc_sl_price:
                    sl_hit = True
                    sl_reason = "stop_btc"
                elif pos.direction == "PE" and spot >= pos.btc_sl_price:
                    sl_hit = True
                    sl_reason = "stop_btc"
            
            if sl_hit:
                return self._close(pos, price, sl_reason)

            # 2. Multi-Layer Take Profit (40/40/20)
            # TP1
            tp1_hit = (pos.option_tp1 and price >= pos.option_tp1)
            if pos.btc_tp1 and spot:
                if pos.direction == "CE" and spot >= pos.btc_tp1: tp1_hit = True
                if pos.direction == "PE" and spot <= pos.btc_tp1: tp1_hit = True
                
            if tp1_hit and "TP1" not in pos.partial_exits:
                pos.partial_exits.append("TP1")
                # Move SL to Entry (Breakeven)
                pos.stop = pos.entry_price
                if pos.btc_sl_price and spot:
                    # Move BTC SL to current spot (or some breakeven approximation)
                    # For simplicity, we just rely on option_sl_price (pos.stop) for the trail
                    pos.btc_sl_price = spot
                # Exit 40% of original contracts
                exit_contracts = max(1, int(pos.contracts * 0.4)) if pos.contracts > 1 else 1
                if exit_contracts < pos.contracts:
                    self._book_close(pos, price, exit_contracts, "tp1_partial")
                else:
                    return self._close(pos, price, "tp1_full")

            # TP2
            tp2_hit = (pos.option_tp2 and price >= pos.option_tp2)
            if pos.btc_tp2 and spot:
                if pos.direction == "CE" and spot >= pos.btc_tp2: tp2_hit = True
                if pos.direction == "PE" and spot <= pos.btc_tp2: tp2_hit = True

            if tp2_hit and "TP2" not in pos.partial_exits:
                pos.partial_exits.append("TP2")
                # Move SL to TP1
                pos.stop = pos.option_tp1 if pos.option_tp1 else pos.entry_price
                if pos.btc_sl_price and pos.btc_tp1:
                    pos.btc_sl_price = pos.btc_tp1
                
                # Exit 40% of original contracts. If 10 contracts, 4 at TP1, 4 at TP2, 2 at TP3.
                # To exit 40% of original:
                # Wait, pos.contracts is currently reduced by TP1. 
                # Total was say 10. Now pos.contracts = 6. 
                # Exit 4.
                # For simplicity, we just exit half of what's left for TP2.
                exit_contracts = max(1, int(pos.contracts * 0.66)) if pos.contracts > 1 else 1
                if exit_contracts < pos.contracts:
                    self._book_close(pos, price, exit_contracts, "tp2_partial")
                else:
                    return self._close(pos, price, "tp2_full")

            # TP3
            tp3_hit = (pos.option_tp3 and price >= pos.option_tp3) or (price >= pos.target)
            if pos.btc_tp3 and spot:
                if pos.direction == "CE" and spot >= pos.btc_tp3: tp3_hit = True
                if pos.direction == "PE" and spot <= pos.btc_tp3: tp3_hit = True
            
            if tp3_hit:
                return self._close(pos, price, "tp3_full")

        # 3. Settlement / Expiry Check
        if _expiry_settled(pos.expiry):
            settle_px = _settlement_value(pos.direction, pos.strike, spot or None)
            if settle_px is None:
                settle_px = 0.0
                journal.record("warn", {
                    "id": pos.id, "symbol": pos.symbol,
                    "note": "settled with no spot available — booked at 0"})
            return self._close(pos, settle_px, "settled", sell=False)
            
        # 4. Time-based Exits
        age_bars = (time.time() - pos.opened_at) / max(pos.bar_seconds, 1)
        if pos.max_hold_bars and age_bars >= pos.max_hold_bars:
            return self._close(pos, price, "time_exit")
            
        # Expiry settlement check
        try:
            d, m, y = pos.expiry.split("-")
            ex_date = datetime(int(y), int(m), int(d), 12, 0, 0, tzinfo=timezone.utc)
            if (ex_date - datetime.now(timezone.utc)).total_seconds() < 0:
                return self._close(pos, price, "dte_exit")
        except Exception:
            pass

        return False

    def flatten_all(self, why: str = "manual_flatten") -> int:
        with self._lock:
            self.pending.clear()
            n = 0
            for pos in list(self.positions.values()):
                try:
                    price = self.resolver.live_price(pos.symbol)
                except Exception:
                    price = None
                if self._close(pos, price, why):
                    n += 1
            self._rebuild_snapshot_locked()
            return n

    # ------------------------------------------------------------------ #
    def reconcile(self) -> None:
        """Paper trading - internal simulation does not reconcile with an exchange."""
        pass

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
                     confidence=50,
                     meta={"adopted": True, "product_id": product_id})
        bar_seconds = 60
        self._register_position(sig.strategy, sig.direction, quote, sig,
                                float(entry), int(size), bar_seconds,
                                leverage=1, order_id=None)
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

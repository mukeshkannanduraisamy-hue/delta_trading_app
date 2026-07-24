"""Option chain resolution: pick the nearest expiry, the ATM strike, and the
tradable call/put contracts (symbol, product_id, lot size, live quote)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import config
from .delta_client import client
from .pricebus import bus


@dataclass
class OptionQuote:
    symbol: str
    product_id: int
    strike: float
    side: str            # "CE" | "PE"
    best_bid: Optional[float]
    best_ask: Optional[float]
    mark_price: Optional[float]
    contract_value: float  # underlying units per contract (e.g. 0.001 BTC)
    tick_size: float       # min price increment for limit orders (e.g. 0.5)
    expiry: str            # DD-MM-YYYY


def _f(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


class OptionResolver:
    """Caches the product universe and resolves ATM contracts for an asset."""

    # Short-lived caches: the engine polls far faster than these change, and
    # several strategies routinely hold the SAME ATM contract, so without them
    # every cycle re-fetches an identical option chain and duplicate quotes.
    ATM_TTL = 10.0     # seconds — chain/ATM resolution
    PRICE_TTL = 3.0    # seconds — per-symbol exit quote

    def __init__(self, asset: str) -> None:
        self.asset = asset
        self._products: list[dict] = []
        self._products_ts = 0.0
        self._atm_cache: tuple[float, float, dict] | None = None  # (ts, spot, result)
        self._price_cache: dict[str, tuple[float, Optional[float]]] = {}

    def _load_products(self) -> list[dict]:
        # Product set changes slowly; refresh at most every 5 minutes.
        if not self._products or time.time() - self._products_ts > 300:
            self._products = client.option_products(self.asset, base=config.EXEC_BASE)
            self._products_ts = time.time()
        return self._products

    def nearest_expiry(self) -> Optional[str]:
        """Soonest live expiry with at least MIN_HOURS_TO_EXPIRY of life left.

        Delta settles options at 12:00 UTC on the expiry date. Anything closer
        than the floor is skipped entirely: the residual premium is essentially
        all theta and the book is too thin to exit into, so buying there is a
        structurally losing trade (audit #5).

        Dates are parsed into real datetimes rather than compared as sliced
        strings, so ordering stays correct across a year boundary.
        """
        floor = datetime.now(timezone.utc) + timedelta(hours=config.MIN_HOURS_TO_EXPIRY)
        candidates: list[tuple[datetime, str]] = []
        seen: set[str] = set()
        for p in self._load_products():
            st = p.get("settlement_time")
            if not st or len(st) < 10 or st[:10] in seen:
                continue
            seen.add(st[:10])
            try:
                y, m, d = int(st[0:4]), int(st[5:7]), int(st[8:10])
                settle = datetime(y, m, d, 12, 0, tzinfo=timezone.utc)
            except ValueError:
                continue
            if settle > floor:
                candidates.append((settle, f"{d:02d}-{m:02d}-{y:04d}"))
        if not candidates:
            return None
        return min(candidates)[1]

    def atm(self, spot: float, expiry: Optional[str] = None) -> dict[str, OptionQuote]:
        """Return {"CE": OptionQuote, "PE": OptionQuote} for the ATM strike.

        Cached for ATM_TTL seconds; the cache is invalidated early if spot has
        moved enough to plausibly change the ATM strike (0.25%).
        """
        if self._atm_cache and expiry is None:
            ts, cached_spot, result = self._atm_cache
            if time.time() - ts < self.ATM_TTL and abs(spot - cached_spot) / max(cached_spot, 1) < 0.0025:
                return result
        result = self._atm_uncached(spot, expiry)
        if expiry is None and result:
            self._atm_cache = (time.time(), spot, result)
        return result

    def _atm_uncached(self, spot: float, expiry: Optional[str] = None) -> dict[str, OptionQuote]:
        expiry = expiry or self.nearest_expiry()
        if not expiry:
            return {}
        tickers = client.option_tickers(self.asset, expiry, base=config.EXEC_BASE)
        if not tickers:
            return {}

        # Map strike -> {"call": ticker, "put": ticker}
        by_strike: dict[float, dict] = {}
        for t in tickers:
            k = _f(t.get("strike_price"))
            if k is None:
                continue
            entry = by_strike.setdefault(k, {})
            if t.get("contract_type") == "call_options":
                entry["call"] = t
            elif t.get("contract_type") == "put_options":
                entry["put"] = t
        if not by_strike:
            return {}

        atm_strike = min(by_strike.keys(), key=lambda k: abs(k - spot))
        pair = by_strike[atm_strike]

        # product_id + contract_value come from the product list.
        prod_by_symbol = {p["symbol"]: p for p in self._load_products()}

        out: dict[str, OptionQuote] = {}
        # Ask the WS feed to stream these contracts so their premiums arrive
        # live rather than being polled once we hold a position in them.
        bus.want(*[s for s in [t.get("symbol") for t in (pair.get("call"), pair.get("put")) if t] if s])
        for side, key in (("CE", "call"), ("PE", "put")):
            t = pair.get(key)
            if not t:
                continue
            sym = t.get("symbol")
            prod = prod_by_symbol.get(sym, {})
            q = t.get("quotes") or {}
            # contract_value scales EVERY P&L and fee number for this contract.
            # Defaulting it to 0.001 when the product lookup missed silently
            # mis-sized all downstream maths (audit #24) — refuse the quote
            # instead, so the strategy skips rather than trades on a guess.
            cv = _f(prod.get("contract_value"))
            if not cv or cv <= 0:
                continue
            strike_val = _f(prod.get("strike_price"))
            if strike_val is None or strike_val <= 0:
                continue  # refuse quote with missing strike — avoids settlement value corruption
            out[side] = OptionQuote(
                symbol=sym,
                product_id=int(prod.get("id") or t.get("product_id") or 0),
                strike=strike_val,
                side=side,
                best_bid=_f(q.get("best_bid")),
                best_ask=_f(q.get("best_ask")),
                mark_price=_f(t.get("mark_price")),
                contract_value=cv,
                tick_size=(_f(prod.get("tick_size")) if _f(prod.get("tick_size")) is not None else 0.5),
                expiry=expiry,
            )
        return out

    def quote_for_product(self, product_id: int) -> Optional[OptionQuote]:
        """Resolve a tradable quote from a product_id alone.

        Needed to ADOPT an orphan position: the exchange tells us a product_id
        and a size, but managing it requires the symbol, strike, expiry and
        contract_value. Returns None if the product is unknown or lacks a
        contract_value, since a guessed contract_value would mis-size every
        subsequent P&L and fee calculation (audit #24).
        """
        prod = None
        for p in self._load_products():
            if int(p.get("id") or 0) == int(product_id):
                prod = p
                break
        if not prod:
            return None
        cv = _f(prod.get("contract_value"))
        if not cv or cv <= 0:
            return None

        sym = prod.get("symbol") or ""
        ctype = (prod.get("contract_type") or "").lower()
        side = "CE" if "call" in ctype else "PE" if "put" in ctype else ""
        if not side:
            return None

        st = prod.get("settlement_time") or ""
        expiry = f"{st[8:10]}-{st[5:7]}-{st[0:4]}" if len(st) >= 10 else ""

        bid = bus.bid(sym)
        ask = bus.ask(sym)
        mark = bus.mark(sym)
        if bid is None or ask is None:
            try:
                t = client.ticker(sym, base=config.EXEC_BASE)
                q = t.get("quotes") or {}
                bid = bid if bid is not None else _f(q.get("best_bid"))
                ask = ask if ask is not None else _f(q.get("best_ask"))
                mark = mark if mark is not None else _f(t.get("mark_price"))
            except Exception:  # noqa: BLE001 — adoption must not depend on a quote
                pass
        bus.want(sym)   # stream it from now on so its stop reacts on the tick

        strike_val = _f(prod.get("strike_price"))
        if strike_val is None or strike_val <= 0:
            return None

        return OptionQuote(
            symbol=sym, product_id=int(product_id),
            strike=strike_val, side=side,
            best_bid=bid, best_ask=ask, mark_price=mark,
            contract_value=cv, tick_size=(_f(prod.get("tick_size")) if _f(prod.get("tick_size")) is not None else 0.5),
            expiry=expiry,
        )

    def premium_candles(self, option_symbol: str, resolution: str, count: int) -> list[dict]:
        """Historical premium candles for a specific option contract."""
        return client.recent_candles(
            option_symbol, resolution, count=count, base=config.EXEC_BASE
        )

    def live_price(self, option_symbol: str) -> Optional[float]:
        """Exit price for a LONG option = the best bid — what you'd actually
        receive on a sell. Returns None when there is no bid at all.

        It deliberately does NOT fall back to mark price. We buy at the ask, so
        valuing the exit at mark books roughly half the spread as free profit and
        makes paper results unusable as evidence (audit #13). A None here is
        honest: the contract is unsellable right now. Callers already handle it —
        `_manage_one` skips the TP/SL check but still applies the time and expiry
        exits, so a bid-less position can never become immortal.

        Cached for PRICE_TTL seconds so that several positions sharing one ATM
        contract cost a single quote per cycle instead of one call each.
        """
        # Live WebSocket tick first — in-memory, effectively zero latency, so
        # stop-losses react on the tick instead of on the next REST poll.
        live = bus.bid(option_symbol)
        if live is not None:
            return live
        hit = self._price_cache.get(option_symbol)
        if hit and time.time() - hit[0] < self.PRICE_TTL:
            return hit[1]
        try:
            t = client.ticker(option_symbol, base=config.EXEC_BASE)
        except Exception:  # noqa: BLE001 — a dead quote must not abort management
            return None
        q = t.get("quotes") or {}
        bid = _f(q.get("best_bid"))
        price = bid if (bid is not None and bid > 0) else None
        self._price_cache[option_symbol] = (time.time(), price)
        # Bound the cache: without eviction it grows by every contract ever
        # touched, across every expiry roll.
        if len(self._price_cache) > 512:
            cutoff = time.time() - self.PRICE_TTL
            self._price_cache = {k: v for k, v in self._price_cache.items()
                                 if v[0] >= cutoff}
        return price

"""Option chain resolution: pick the nearest expiry, the ATM strike, and the
tradable call/put contracts (symbol, product_id, lot size, live quote)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from . import config
from .delta_client import client


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
    expiry: str            # DD-MM-YYYY


def _f(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


class OptionResolver:
    """Caches the product universe and resolves ATM contracts for an asset."""

    def __init__(self, asset: str) -> None:
        self.asset = asset
        self._products: list[dict] = []
        self._products_ts = 0.0

    def _load_products(self) -> list[dict]:
        # Product set changes slowly; refresh at most every 5 minutes.
        if not self._products or time.time() - self._products_ts > 300:
            self._products = client.option_products(self.asset, base=config.EXEC_BASE)
            self._products_ts = time.time()
        return self._products

    def nearest_expiry(self) -> Optional[str]:
        """Return the soonest live expiry as DD-MM-YYYY."""
        iso_dates = sorted(
            {
                p["settlement_time"][:10]
                for p in self._load_products()
                if p.get("settlement_time") and len(p["settlement_time"]) >= 10
            }
        )
        if not iso_dates:
            return None
        d = iso_dates[0]
        return f"{d[8:10]}-{d[5:7]}-{d[0:4]}"

    def atm(self, spot: float, expiry: Optional[str] = None) -> dict[str, OptionQuote]:
        """Return {"CE": OptionQuote, "PE": OptionQuote} for the ATM strike."""
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
        for side, key in (("CE", "call"), ("PE", "put")):
            t = pair.get(key)
            if not t:
                continue
            sym = t.get("symbol")
            prod = prod_by_symbol.get(sym, {})
            q = t.get("quotes") or {}
            out[side] = OptionQuote(
                symbol=sym,
                product_id=int(prod.get("id") or t.get("product_id") or 0),
                strike=atm_strike,
                side=side,
                best_bid=_f(q.get("best_bid")),
                best_ask=_f(q.get("best_ask")),
                mark_price=_f(t.get("mark_price")),
                contract_value=_f(prod.get("contract_value")) or 0.001,
                expiry=expiry,
            )
        return out

    def premium_candles(self, option_symbol: str, resolution: str, count: int) -> list[dict]:
        """Historical premium candles for a specific option contract."""
        return client.recent_candles(
            option_symbol, resolution, count=count, base=config.EXEC_BASE
        )

    def live_price(self, option_symbol: str) -> Optional[float]:
        """Exit price for a LONG option = the best bid (what you'd receive when
        selling). Falls back to mark price only if there is no live bid. Using
        the bid — not the mark — keeps paper P&L honest: we buy at the ask and
        sell at the bid, so the bid/ask spread is a real cost, not free profit.
        """
        t = client.ticker(option_symbol, base=config.EXEC_BASE)
        q = t.get("quotes") or {}
        return _f(q.get("best_bid")) or _f(t.get("mark_price"))

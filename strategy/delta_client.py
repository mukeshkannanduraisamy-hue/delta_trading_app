"""Signed REST client for Delta Exchange India.

Public market-data calls hit the production base; authenticated calls (orders,
balances, positions) hit the testnet demo base and are HMAC-signed with the
demo API keys. Signing scheme (verified live 2026-07-18):

    signature = HMAC_SHA256(secret, method + timestamp + path + query + body)

sent as the `signature` header alongside `api-key` and `timestamp`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Optional

import requests

from . import config


class DeltaError(RuntimeError):
    pass


class DeltaClient:
    def __init__(self) -> None:
        self.key = config.API_KEY
        self.secret = config.API_SECRET
        self.prod = config.PROD_BASE
        self.testnet = config.TESTNET_BASE
        self.session = requests.Session()

    # ------------------------------------------------------------------ #
    # Signing
    # ------------------------------------------------------------------ #
    def _headers(self, method: str, path: str, query: str, body: str) -> dict:
        ts = str(int(time.time()))
        prehash = method + ts + path + query + body
        sig = hmac.new(
            self.secret.encode(), prehash.encode(), hashlib.sha256
        ).hexdigest()
        return {
            "api-key": self.key,
            "timestamp": ts,
            "signature": sig,
            "User-Agent": "delta-phase4-engine",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------ #
    # Low-level requests
    # ------------------------------------------------------------------ #
    def _public_get(self, base: str, path: str, params: Optional[dict] = None) -> Any:
        r = self.session.get(base + path, params=params or {}, timeout=20)
        r.raise_for_status()
        return r.json().get("result")

    def _signed(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        payload: Optional[dict] = None,
    ) -> Any:
        if not (self.key and self.secret):
            raise DeltaError("API keys are not configured (.env)")
        query = ""
        if params:
            query = "?" + "&".join(f"{k}={v}" for k, v in params.items())
        body = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
        headers = self._headers(method, path, query, body)
        url = self.testnet + path + query
        r = self.session.request(method, url, headers=headers, data=body, timeout=20)
        try:
            data = r.json()
        except ValueError:
            raise DeltaError(f"{r.status_code}: {r.text[:200]}")
        if not r.ok or (isinstance(data, dict) and data.get("success") is False):
            raise DeltaError(
                f"{r.status_code}: {json.dumps(data.get('error', data))[:300]}"
            )
        return data.get("result")

    # ------------------------------------------------------------------ #
    # Public market data
    # ------------------------------------------------------------------ #
    def candles(
        self, symbol: str, resolution: str, start: int, end: int, base: Optional[str] = None
    ) -> list[dict]:
        """Historical OHLC candles (returned newest-first by Delta -> reversed)."""
        rows = self._public_get(
            base or self.prod,
            "/v2/history/candles",
            {"symbol": symbol, "resolution": resolution, "start": start, "end": end},
        ) or []
        rows.sort(key=lambda c: c.get("time", 0))
        return rows

    def recent_candles(
        self, symbol: str, resolution: str, count: int = 120, base: Optional[str] = None
    ) -> list[dict]:
        unit = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
        secs = int(resolution[:-1]) * unit.get(resolution[-1], 60)
        end = int(time.time())
        start = end - secs * (count + 5)
        return self.candles(symbol, resolution, start, end, base=base)[-count:]

    def option_products(self, asset: str, base: Optional[str] = None) -> list[dict]:
        """Live call+put option products for an underlying (id, symbol, strike...)."""
        out: list[dict] = []
        for ct in ("call_options", "put_options"):
            rows = self._public_get(
                base or config.EXEC_BASE,
                "/v2/products",
                {
                    "contract_types": ct,
                    "underlying_asset_symbol": asset,
                    "states": "live",
                },
            ) or []
            out.extend(rows)
        return out

    def option_tickers(self, asset: str, expiry_ddmmyyyy: str, base: Optional[str] = None) -> list[dict]:
        return self._public_get(
            base or config.EXEC_BASE,
            "/v2/tickers",
            {
                "contract_types": "call_options,put_options",
                "underlying_asset_symbols": asset,
                "expiry_date": expiry_ddmmyyyy,
            },
        ) or []

    def ticker(self, symbol: str, base: Optional[str] = None) -> dict:
        return self._public_get(base or config.EXEC_BASE, f"/v2/tickers/{symbol}") or {}

    # ------------------------------------------------------------------ #
    # Authenticated (testnet demo)
    # ------------------------------------------------------------------ #
    def profile(self) -> dict:
        return self._signed("GET", "/v2/profile")

    def balances(self) -> list[dict]:
        return self._signed("GET", "/v2/wallet/balances")

    def positions(self) -> list[dict]:
        return self._signed("GET", "/v2/positions/margined")

    def place_order(
        self,
        product_id: int,
        size: int,
        side: str,
        order_type: str = "market_order",
        limit_price: Optional[float] = None,
    ) -> dict:
        payload: dict = {
            "product_id": product_id,
            "size": size,
            "side": side,  # "buy" | "sell"
            "order_type": order_type,  # "market_order" | "limit_order"
        }
        if order_type == "limit_order" and limit_price is not None:
            payload["limit_price"] = str(limit_price)
        return self._signed("POST", "/v2/orders", payload=payload)


client = DeltaClient()

"""Signed REST client for Delta Exchange India.

Public market-data calls hit the production base; authenticated calls (orders,
balances, positions) hit the testnet demo base and are HMAC-signed with the
demo API keys. Signing scheme (verified live 2026-07-18):

    signature = HMAC_SHA256(secret, method + timestamp + path + query + body)

sent as the `signature` header alongside `api-key` and `timestamp`.

Error contract (audit 2026-07-21). Callers get a *typed* failure so they can
react correctly instead of treating every problem as the same:

  DeltaRateLimited   HTTP 429 — carries `retry_after` from X-RATE-LIMIT-RESET.
                     Back off for that long; retrying sooner extends the ban.
  DeltaAuthError     401 (bad key / bad signature / IP not whitelisted).
                     PERMANENT — retrying cannot fix it.
  DeltaNetworkError  Timeout or connection drop. The outcome is UNKNOWN: the
                     exchange may have accepted the order. Reconcile by
                     client_order_id before retrying, never resend blindly.
  DeltaError         Definitive rejection (4xx with a body, or success:false).

Timestamps are regenerated for EVERY attempt because a Delta signature is only
valid for 5 seconds — a cached one would fail on any retry.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import random
import time
from typing import Any, Optional
from urllib.parse import urlencode

import requests

from . import config

# (connect, read). The read timeout is deliberately short on signed calls: the
# executor holds its position lock across these, so a 20s hang there froze the
# whole app (audit #2). Public market data may take longer — nothing waits on it.
TIMEOUT_SIGNED = (3.05, 12)
TIMEOUT_PUBLIC = (3.05, 20)

_MAX_5XX_RETRIES = 3
# Delta returns at most this many candles per /v2/history/candles call.
_CANDLE_PAGE = 2000


class DeltaError(RuntimeError):
    """The API rejected the request (a definitive failure)."""


class DeltaNetworkError(DeltaError):
    """The request may or may not have reached the exchange (timeout /
    connection drop). Callers placing orders MUST treat the outcome as
    unknown and reconcile before retrying, or they risk double-executing."""


class DeltaRateLimited(DeltaError):
    """HTTP 429. `retry_after` is seconds until the quota window resets."""

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class DeltaAuthError(DeltaError):
    """Permanent auth failure (bad key, bad signature, IP not whitelisted).
    Never retry — the request cannot succeed until an operator intervenes."""


def _retry_after(resp) -> float:
    """Seconds until the rate-limit window resets.

    Delta sends X-RATE-LIMIT-RESET in MILLISECONDS; the standard Retry-After is
    in seconds. Values above 1000 are therefore milliseconds.
    """
    raw = resp.headers.get("X-RATE-LIMIT-RESET") or resp.headers.get("Retry-After")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 5.0
    secs = v / 1000.0 if v > 1000 else v
    return min(max(secs, 1.0), 300.0)


def _backoff(attempt: int) -> float:
    """Exponential backoff with jitter, so concurrent callers don't resynchronize."""
    return (2 ** attempt) * (0.5 + random.random())


class DeltaClient:
    def __init__(self) -> None:
        self.key = config.API_KEY
        self.secret = config.API_SECRET
        self.prod = config.PROD_BASE
        self.testnet = config.TESTNET_BASE
        self.session = requests.Session()

    def _headers(self, method: str, path: str, query: str, body: str) -> dict:
        return {
            "User-Agent": "delta-phase4-engine",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------ #
    # Low-level requests
    # ------------------------------------------------------------------ #
    def _public_get(self, base: str, path: str, params: Optional[dict] = None) -> Any:
        try:
            r = self.session.get(base + path, params=params or {}, timeout=TIMEOUT_PUBLIC)
        except requests.RequestException as exc:
            raise DeltaNetworkError(
                f"network failure ({exc.__class__.__name__}): {exc}"
            ) from exc
        if r.status_code == 429:
            raise DeltaRateLimited(f"429 rate limited on {path}", _retry_after(r))
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError:
            raise DeltaError(f"{path}: non-JSON response {r.text[:200]}")
        # A 200 with success:false is still a failure — returning .get("result")
        # blindly would hand callers None and look like "no data".
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            return data
        if data.get("success") is False:
            raise DeltaError(f"{url}: {data.get('error', data)}")
        return data.get("result", data)

    def _signed(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        payload: Optional[dict] = None,
        _retried_sig: bool = False,
        retries: Optional[int] = None,
    ) -> Any:
        raise NotImplementedError("Authenticated requests are disabled in paper trading mode.")

    # ------------------------------------------------------------------ #
    # Public market data
    # ------------------------------------------------------------------ #
    def candles(
        self, symbol: str, resolution: str, start: int, end: int,
        base: Optional[str] = None, max_pages: int = 20,
    ) -> list[dict]:
        """Historical OHLC candles, oldest-first.

        Delta caps a response at ~2000 bars, so a window wider than that silently
        truncated before (audit #23) and left indicators short of warm-up. Page
        backwards until the window is covered.

        `start`/`end` are Unix SECONDS, not milliseconds.
        """
        secs = _resolution_seconds(resolution)
        out: dict[int, dict] = {}
        cursor = int(end)
        start = int(start)
        for _ in range(max_pages):
            win_start = max(start, cursor - secs * _CANDLE_PAGE)
            rows = self._public_get(
                base or self.prod,
                "/v2/history/candles",
                {"symbol": symbol, "resolution": resolution,
                 "start": win_start, "end": cursor},
            ) or []
            if not rows:
                break
            times = [int(r["time"]) for r in rows if r.get("time") is not None]
            if not times:
                break
            for r in rows:
                if r.get("time") is not None:
                    out[int(r["time"])] = r
            oldest = min(times)
            if oldest <= start or win_start <= start:
                break
            cursor = oldest - secs
        return [out[t] for t in sorted(out)]

    def recent_candles(
        self, symbol: str, resolution: str, count: int = 120, base: Optional[str] = None
    ) -> list[dict]:
        secs = _resolution_seconds(resolution)
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

    def l2orderbook(self, symbol: str, base: Optional[str] = None) -> dict:
        """Fetch L2 orderbook for a symbol (level 2 depth)."""
        return self._public_get(base or config.EXEC_BASE, f"/v2/l2orderbook/{symbol}") or {}

    # ------------------------------------------------------------------ #
    # Authenticated (Disabled for Paper Trading)
    # ------------------------------------------------------------------ #
    def profile(self, retries: Optional[int] = None) -> dict:
        raise NotImplementedError()

    def balances(self) -> list[dict]:
        raise NotImplementedError()

    def positions(self, product_id: Optional[int] = None) -> list[dict]:
        raise NotImplementedError()

    def place_order(self, *args, **kwargs) -> dict:
        raise NotImplementedError()

    def open_orders(self, product_id: Optional[int] = None) -> list[dict]:
        raise NotImplementedError()

    def get_order(self, order_id: int) -> dict:
        raise NotImplementedError()

    def get_order_by_coid(self, client_order_id: str) -> dict:
        raise NotImplementedError()

    def cancel_order(self, order_id: int, product_id: int) -> dict:
        raise NotImplementedError()

    def rate_limit_quota(self) -> dict:
        raise NotImplementedError()


_UNIT = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def _resolution_seconds(resolution: str) -> int:
    try:
        return int(resolution[:-1]) * _UNIT.get(resolution[-1], 60)
    except (ValueError, IndexError):
        return 60


client = DeltaClient()

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

    # ------------------------------------------------------------------ #
    # Signing
    # ------------------------------------------------------------------ #
    def _headers(self, method: str, path: str, query: str, body: str) -> dict:
        # Fresh timestamp on every call: a Delta signature expires after 5s.
        ts = str(int(time.time()))
        prehash = method + ts + path + query + body
        sig = hmac.new(
            self.secret.encode(), prehash.encode(), hashlib.sha256
        ).hexdigest()
        return {
            "api-key": self.key,
            "timestamp": ts,
            "signature": sig,
            # Delta rejects requests without a User-Agent with an opaque 4XX.
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
        if isinstance(data, dict) and data.get("success") is False:
            raise DeltaError(f"{path}: {json.dumps(data.get('error', data))[:300]}")
        return data.get("result")

    def _signed(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        payload: Optional[dict] = None,
        _retried_sig: bool = False,
        retries: Optional[int] = None,
    ) -> Any:
        """`retries` bounds 5XX attempts. Default is _MAX_5XX_RETRIES, which is
        right for orders. Pass 1 for latency-sensitive probes (health polling),
        where waiting through a full backoff ladder during an upstream outage is
        worse than reporting the failure immediately."""
        if not (self.key and self.secret):
            raise DeltaError("API keys are not configured (.env)")
        # urlencode once and reuse the exact same string in both the signed
        # prehash and the URL, so encoding can never diverge between them.
        query = "?" + urlencode(params) if params else ""
        body = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
        url = self.testnet + path + query

        max_attempts = _MAX_5XX_RETRIES if retries is None else max(1, int(retries))
        last_exc: Optional[DeltaError] = None
        for attempt in range(max_attempts):
            headers = self._headers(method, path, query, body)
            try:
                r = self.session.request(
                    method, url, headers=headers, data=body, timeout=TIMEOUT_SIGNED
                )
            except requests.RequestException as exc:
                # Outcome unknown: the order may have been accepted before the
                # connection died. Callers must reconcile, not blindly retry.
                raise DeltaNetworkError(
                    f"network failure ({exc.__class__.__name__}): {exc}"
                ) from exc

            if r.status_code == 429:
                raise DeltaRateLimited(f"429 rate limited on {path}", _retry_after(r))

            try:
                data = r.json()
            except ValueError:
                if 500 <= r.status_code < 600 and attempt < max_attempts - 1:
                    last_exc = DeltaError(f"{r.status_code}: {r.text[:200]}")
                    time.sleep(_backoff(attempt))
                    continue
                raise DeltaError(f"{r.status_code}: {r.text[:200]}")

            err = (data.get("error") or {}) if isinstance(data, dict) else {}
            code = err.get("code") if isinstance(err, dict) else None

            if r.status_code == 401 or code in (
                "SignatureExpired", "expired_signature", "invalid_signature",
                "ip_not_whitelisted_for_api_key", "api_key_not_found",
            ):
                if code in ("SignatureExpired", "expired_signature") and not _retried_sig:
                    # Clock drift or a slow hop consumed the 5s validity window.
                    # Retry ONCE with a freshly generated timestamp+signature.
                    return self._signed(method, path, params, payload,
                                        _retried_sig=True, retries=retries)
                if code == "ip_not_whitelisted_for_api_key":
                    ctx = err.get("context") or {}
                    ip = ctx.get("client_ip") or ctx.get("ip") or "unknown"
                    raise DeltaAuthError(
                        f"ip_not_whitelisted_for_api_key — whitelist {ip} "
                        f"on demo.delta.exchange"
                    )
                raise DeltaAuthError(f"{r.status_code}: {json.dumps(err or data)[:300]}")

            if 500 <= r.status_code < 600 and attempt < _MAX_5XX_RETRIES - 1:
                last_exc = DeltaError(f"{r.status_code}: {json.dumps(data)[:300]}")
                time.sleep(_backoff(attempt))
                continue

            if not r.ok or (isinstance(data, dict) and data.get("success") is False):
                raise DeltaError(f"{r.status_code}: {json.dumps(err or data)[:300]}")
            return data.get("result")

        raise last_exc or DeltaError(f"exhausted retries on {path}")

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

    # ------------------------------------------------------------------ #
    # Authenticated (testnet demo)
    # ------------------------------------------------------------------ #
    def profile(self, retries: Optional[int] = None) -> dict:
        return self._signed("GET", "/v2/profile", retries=retries)

    def balances(self) -> list[dict]:
        return self._signed("GET", "/v2/wallet/balances")

    def positions(self, product_id: Optional[int] = None) -> list[dict]:
        """Open positions.

        With `product_id` this hits /v2/positions, which is REAL-TIME and is the
        only form safe to base an order decision on. Without it, it falls back to
        /v2/positions/margined, which Delta documents as lagging up to 10 seconds
        — fine for a periodic audit, never for deciding whether to send a sell
        (audit #1: a stale "still open" reading caused a double-sell).
        """
        if product_id is not None:
            rows = self._signed("GET", "/v2/positions",
                                params={"product_id": int(product_id)})
            if isinstance(rows, dict):     # single-product form returns an object
                return [rows] if rows else []
            return rows or []
        return self._signed("GET", "/v2/positions/margined") or []

    def place_order(
        self,
        product_id: int,
        size: int,
        side: str,
        order_type: str = "market_order",
        limit_price: Optional[float] = None,
        reduce_only: bool = False,
        client_order_id: Optional[str] = None,
        time_in_force: str = "gtc",
    ) -> dict:
        """Place a single order.

        Field rules Delta enforces (violations are silent bugs or opaque 400s):
          * size must be a positive INTEGER
          * limit_price must be a STRING
          * reduce_only must be the STRING "true"/"false", not a JSON boolean
          * client_order_id is capped at 32 chars and is our idempotency key
          * send product_id OR product_symbol, never both
        """
        if side not in ("buy", "sell"):
            raise DeltaError(f"invalid side {side!r}")
        if order_type not in ("limit_order", "market_order"):
            raise DeltaError(f"invalid order_type {order_type!r}")
        size = int(size)
        if size <= 0:
            raise DeltaError(f"size must be a positive integer, got {size!r}")

        payload: dict = {
            "product_id": int(product_id),
            "size": size,
            "side": side,
            "order_type": order_type,
            # String form is required. With reduce_only a sell can only ever
            # SHRINK a long — it can never flip the account short, which is what
            # made an ambiguous double-send dangerous.
            "reduce_only": "true" if reduce_only else "false",
        }
        if order_type == "limit_order":
            if limit_price is None:
                raise DeltaError("limit_order requires limit_price")
            payload["limit_price"] = str(limit_price)
            payload["time_in_force"] = time_in_force
        if client_order_id:
            payload["client_order_id"] = str(client_order_id)[:32]
        return self._signed("POST", "/v2/orders", payload=payload)

    def open_orders(self, product_id: Optional[int] = None) -> list[dict]:
        """Resting (open/pending) orders. Part of the account sync: an order we
        are not tracking can still fill and create an untracked position."""
        params = {"states": "open,pending"}
        if product_id is not None:
            params["product_id"] = int(product_id)
        return self._signed("GET", "/v2/orders", params=params) or []

    def get_order(self, order_id: int) -> dict:
        """Fetch a single order's current state (fill progress)."""
        return self._signed("GET", f"/v2/orders/{order_id}") or {}

    def get_order_by_coid(self, client_order_id: str) -> dict:
        """Look up an order by OUR idempotency key.

        This is how an ambiguous (timed-out) submission is resolved: if the
        exchange has the order, it landed; a 404 proves it never did. Raises
        DeltaError on 404, DeltaNetworkError if still unreachable.
        """
        return self._signed(
            "GET", f"/v2/orders/client_order_id/{str(client_order_id)[:32]}"
        ) or {}

    def cancel_order(self, order_id: int, product_id: int) -> dict:
        """Cancel a resting order (Delta wants both id and product_id)."""
        return self._signed(
            "DELETE", "/v2/orders",
            payload={"id": int(order_id), "product_id": int(product_id)},
        ) or {}

    def rate_limit_quota(self) -> dict:
        """Remaining quota in the current 5-minute window."""
        return self._signed("GET", "/v2/rate_limits/quota") or {}


_UNIT = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def _resolution_seconds(resolution: str) -> int:
    try:
        return int(resolution[:-1]) * _UNIT.get(resolution[-1], 60)
    except (ValueError, IndexError):
        return 60


client = DeltaClient()

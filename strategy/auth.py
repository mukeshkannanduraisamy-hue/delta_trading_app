"""Authentication for state-changing endpoints.

WHY MIDDLEWARE AND NOT per-endpoint dependencies
------------------------------------------------
The app exposes POST routes that start and stop the engine, flatten positions,
rewrite settings and open orders. Protecting them with a `Depends(...)` on each
route means a future endpoint is unprotected until someone remembers to add it.
Here the rule is "every state-changing /api call needs auth" and it is enforced
in one place, so new routes are covered by default and have to be explicitly
allow-listed to be public.

THE POLICY
----------
* GET / HEAD / OPTIONS      -> always allowed (read-only)
* requests from localhost   -> always allowed (unchanged local development)
* everything else           -> requires the shared key

FAIL CLOSED
-----------
If APP_API_KEY is not set, remote state-changing requests are REFUSED rather
than allowed. Deploying without configuring a key must not silently expose the
trading engine to anyone who finds the URL — that is precisely the failure this
exists to prevent.
"""

from __future__ import annotations

import hmac
import ipaddress
import os

from fastapi import Request
from fastapi.responses import JSONResponse

API_KEY_ENV = "APP_API_KEY"
HEADER = "x-api-key"
COOKIE = "app_api_key"

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Public POST routes. Keep this list as short as possible and justify additions.
PUBLIC_PATHS = {
    "/api/auth",          # exchanges the key for a cookie; checks the key itself
}


def configured_key() -> str | None:
    key = os.environ.get(API_KEY_ENV, "").strip()
    return key or None


def is_local(request: Request) -> bool:
    """True for loopback callers only.

    Note this trusts the socket peer, NOT X-Forwarded-For — a header a remote
    caller can set freely. Behind a reverse proxy every request appears local,
    so a proxied deployment must set APP_API_KEY and terminate auth at the
    proxy, or run the app on an interface the proxy alone can reach.
    """
    client = request.client
    if client is None or not client.host:
        return False
    try:
        return ipaddress.ip_address(client.host).is_loopback
    except ValueError:
        return client.host in {"localhost", "testclient"}


def presented_key(request: Request) -> str | None:
    return (request.headers.get(HEADER)
            or request.cookies.get(COOKIE)
            or None)


def key_matches(presented: str | None) -> bool:
    """Constant-time comparison, so the check cannot be probed byte by byte."""
    expected = configured_key()
    if not expected or not presented:
        return False
    return hmac.compare_digest(presented, expected)


def requires_auth(method: str, path: str) -> bool:
    if method.upper() in SAFE_METHODS:
        return False
    if path in PUBLIC_PATHS:
        return False
    return path.startswith("/api/")


async def auth_middleware(request: Request, call_next):
    if not requires_auth(request.method, request.url.path):
        return await call_next(request)

    if is_local(request):
        return await call_next(request)

    if configured_key() is None:
        return JSONResponse(
            status_code=503,
            content={"error": "This endpoint changes server state and is "
                              "reachable remotely, but APP_API_KEY is not "
                              "configured. Set it to enable remote control.",
                     "hint": f"set {API_KEY_ENV} in the environment"})

    if not key_matches(presented_key(request)):
        return JSONResponse(
            status_code=401,
            content={"error": "missing or invalid API key",
                     "hint": f"send it as the {HEADER} header, or POST it to "
                             f"/api/auth to receive a session cookie"})

    return await call_next(request)


def install(app) -> None:
    app.middleware("http")(auth_middleware)

    @app.post("/api/auth")
    async def api_auth(request: Request):
        """Exchange the shared key for a cookie so the browser UI can work."""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — a malformed body is just a bad key
            body = {}
        supplied = body.get("key") or request.headers.get(HEADER)
        if not configured_key():
            return JSONResponse(status_code=503,
                                content={"error": f"{API_KEY_ENV} is not set"})
        if not key_matches(supplied):
            return JSONResponse(status_code=401, content={"error": "invalid key"})
        resp = JSONResponse(content={"ok": True})
        resp.set_cookie(COOKIE, supplied, httponly=True, samesite="strict",
                        max_age=7 * 24 * 3600)
        return resp

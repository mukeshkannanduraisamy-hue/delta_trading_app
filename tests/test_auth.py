import asyncio

import pytest

from strategy import auth


class _Req:
    """Minimal stand-in for a Starlette Request."""
    def __init__(self, method="POST", path="/api/strategy/start", host="1.2.3.4",
                 headers=None, cookies=None):
        self.method = method
        self.url = type("U", (), {"path": path})()
        self.client = type("C", (), {"host": host})() if host else None
        self.headers = headers or {}
        self.cookies = cookies or {}


# ---------------- which requests need auth ----------------

def test_read_only_methods_are_never_gated():
    for m in ("GET", "HEAD", "OPTIONS"):
        assert auth.requires_auth(m, "/api/strategy/start") is False


def test_every_state_changing_api_route_is_gated_by_default():
    """The reason this is middleware: a new POST route must be protected
    without anyone remembering to guard it."""
    for path in ("/api/strategy/start", "/api/strategy/stop",
                 "/api/strategy/toggle", "/api/strategy/flatten",
                 "/api/strategy/flatten-shorts", "/api/settings",
                 "/api/order/place", "/api/some/future/endpoint"):
        assert auth.requires_auth("POST", path) is True, path


def test_non_api_paths_are_not_gated():
    assert auth.requires_auth("POST", "/login") is False


def test_auth_endpoint_itself_is_public():
    assert auth.requires_auth("POST", "/api/auth") is False


# ---------------- localhost policy ----------------

def test_loopback_is_treated_as_local():
    for host in ("127.0.0.1", "::1", "127.0.0.5"):
        assert auth.is_local(_Req(host=host)) is True


def test_remote_addresses_are_not_local():
    for host in ("1.2.3.4", "10.0.0.5", "192.168.1.9"):
        assert auth.is_local(_Req(host=host)) is False


def test_missing_client_is_not_treated_as_local():
    """Fail closed when the peer cannot be determined."""
    assert auth.is_local(_Req(host=None)) is False


def test_forwarded_header_cannot_fake_localhost():
    """X-Forwarded-For is attacker-controlled; only the socket peer counts."""
    r = _Req(host="1.2.3.4", headers={"x-forwarded-for": "127.0.0.1"})
    assert auth.is_local(r) is False


# ---------------- key checking ----------------

def test_missing_key_config_never_matches(monkeypatch):
    monkeypatch.delenv(auth.API_KEY_ENV, raising=False)
    assert auth.key_matches("anything") is False


def test_correct_key_matches(monkeypatch):
    monkeypatch.setenv(auth.API_KEY_ENV, "s3cret")
    assert auth.key_matches("s3cret") is True


def test_wrong_key_rejected(monkeypatch):
    monkeypatch.setenv(auth.API_KEY_ENV, "s3cret")
    assert auth.key_matches("s3cre") is False
    assert auth.key_matches("s3cret ") is False
    assert auth.key_matches(None) is False


def test_empty_env_key_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv(auth.API_KEY_ENV, "   ")
    assert auth.configured_key() is None
    assert auth.key_matches("") is False


def test_key_read_from_header_or_cookie():
    assert auth.presented_key(_Req(headers={auth.HEADER: "h"})) == "h"
    assert auth.presented_key(_Req(cookies={auth.COOKIE: "c"})) == "c"
    assert auth.presented_key(_Req()) is None


# ---------------- the middleware ----------------

def test_remote_request_without_key_config_fails_closed(monkeypatch):
    """Deploying without setting a key must NOT silently expose the engine."""
    monkeypatch.delenv(auth.API_KEY_ENV, raising=False)
    called = []

    async def nxt(_):
        called.append(1)
        return "reached endpoint"

    resp = asyncio.run(auth.auth_middleware(_Req(host="1.2.3.4"), nxt))
    assert not called, "the endpoint must not run"
    assert resp.status_code == 503


def test_remote_request_with_wrong_key_is_rejected(monkeypatch):
    monkeypatch.setenv(auth.API_KEY_ENV, "s3cret")
    called = []

    async def nxt(_):
        called.append(1)
        return "reached endpoint"

    resp = asyncio.run(auth.auth_middleware(
        _Req(host="1.2.3.4", headers={auth.HEADER: "nope"}), nxt))
    assert not called
    assert resp.status_code == 401


def test_remote_request_with_correct_key_passes(monkeypatch):
    monkeypatch.setenv(auth.API_KEY_ENV, "s3cret")

    async def nxt(_):
        return "reached endpoint"

    got = asyncio.run(auth.auth_middleware(
        _Req(host="1.2.3.4", headers={auth.HEADER: "s3cret"}), nxt))
    assert got == "reached endpoint"


def test_localhost_passes_without_any_key(monkeypatch):
    """Local development must be unchanged by this feature."""
    monkeypatch.delenv(auth.API_KEY_ENV, raising=False)

    async def nxt(_):
        return "reached endpoint"

    got = asyncio.run(auth.auth_middleware(_Req(host="127.0.0.1"), nxt))
    assert got == "reached endpoint"


def test_remote_GET_passes_without_a_key(monkeypatch):
    monkeypatch.delenv(auth.API_KEY_ENV, raising=False)

    async def nxt(_):
        return "reached endpoint"

    got = asyncio.run(auth.auth_middleware(
        _Req(method="GET", path="/api/status", host="1.2.3.4"), nxt))
    assert got == "reached endpoint"

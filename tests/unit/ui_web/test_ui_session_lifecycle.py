"""Browser session refresh/logout: explicit, cookie-only and origin-gated."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jarvis.ui.web import missions_auth, surface_security
from jarvis.ui.web.surface_security import COOKIE_NAME, SurfaceSecurity

pytestmark = pytest.mark.no_auto_web_auth

_BASE = "http://127.0.0.1:47821"
_ORIGIN = {"Origin": _BASE}
_KEY = "test-ui-control-key-not-a-secret"


@pytest.fixture(autouse=True)
def reset_state():
    missions_auth.reset_tokens()
    surface_security.set_browser_login_required(True)
    yield
    missions_auth.reset_tokens()
    surface_security.reset_browser_login_required()


def _app() -> SurfaceSecurity:
    from fastapi.responses import JSONResponse

    app = FastAPI()

    @app.get("/api/probe")
    async def probe():
        return JSONResponse({"ok": True})

    app.include_router(missions_auth.router)
    return SurfaceSecurity(app, control_key_validator=lambda key: key == _KEY)


def _cookie(response) -> str:
    cookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    return cookie[COOKIE_NAME].value


def _login(client: TestClient) -> str:
    response = client.post("/api/ui/session", json={"control_key": _KEY}, headers=_ORIGIN)
    assert response.status_code == 204
    return _cookie(response)


def test_function_refresh_replaces_cookie_and_revokes_previous_session():
    with TestClient(_app(), base_url=_BASE, client=("127.0.0.1", 49998)) as client:
        original = _login(client)
        assert client.get("/api/probe").status_code == 200
        response = client.post("/api/ui/session/refresh", headers=_ORIGIN)
        assert response.status_code == 204
        assert response.headers["cache-control"] == "no-store"
        refreshed = _cookie(response)
        assert refreshed != original
        assert missions_auth.validate_token(refreshed)
        assert not missions_auth.validate_token(original)
        assert client.get("/api/probe").status_code == 200
        assert client.get("/api/probe", headers={"Authorization": f"Bearer {original}"}).status_code == 401


def test_function_logout_revokes_token_and_expires_cookie():
    with TestClient(_app(), base_url=_BASE, client=("127.0.0.1", 49998)) as client:
        original = _login(client)
        response = client.post("/api/ui/session/logout", headers=_ORIGIN)
        assert response.status_code == 204
        assert response.headers["cache-control"] == "no-store"
        expired_cookie = SimpleCookie()
        expired_cookie.load(response.headers["set-cookie"])
        morsel = expired_cookie[COOKIE_NAME]
        assert morsel.value == ""
        assert morsel["max-age"] == "0"
        assert morsel["httponly"] is True
        assert morsel["samesite"].lower() == "strict"
        assert not missions_auth.validate_token(original)
        assert client.get("/api/probe").status_code == 401
        assert client.post("/api/ui/session/logout", headers=_ORIGIN).status_code == 401


@pytest.mark.parametrize("route", ["refresh", "logout"])
def test_security_requires_same_origin_and_cannot_use_bearer(route: str):
    with TestClient(_app(), base_url=_BASE, client=("127.0.0.1", 49998)) as client:
        original = _login(client)
        path = f"/api/ui/session/{route}"
        assert client.post(path, headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.post(path, headers={"Host": "127.0.0.1:47821"}).status_code == 403
        assert client.post(path, headers={**_ORIGIN, "Authorization": f"Bearer {_KEY}"}).status_code == 401
        assert missions_auth.validate_token(original)
        assert client.get("/api/probe").status_code == 200


@pytest.mark.parametrize("route", ["refresh", "logout"])
def test_security_requires_actual_cookie_not_open_access_or_control_key(route: str):
    surface_security.set_browser_login_required(False)
    with TestClient(_app(), base_url=_BASE, client=("127.0.0.1", 49998)) as client:
        path = f"/api/ui/session/{route}"
        assert client.post(path, headers=_ORIGIN).status_code == 401
        assert client.post(path, headers={**_ORIGIN, "Authorization": f"Bearer {_KEY}"}).status_code == 401


def test_integration_refresh_allows_websocket_ticket_mint_but_logout_blocks_it():
    with TestClient(_app(), base_url=_BASE, client=("127.0.0.1", 49998)) as client:
        old = _login(client)
        assert client.post("/api/ui/session/refresh", headers=_ORIGIN).status_code == 204
        response = client.get("/api/missions/auth/token", headers=_ORIGIN)
        assert response.status_code == 200
        ws_ticket = response.json()["token"]
        assert missions_auth.consume_ws_hello_token(ws_ticket)
        assert not missions_auth.validate_token(old)
        assert client.post("/api/ui/session/logout", headers=_ORIGIN).status_code == 204
        assert client.get("/api/missions/auth/token", headers=_ORIGIN).status_code == 401


def test_security_concurrent_replays_can_rotate_only_once():
    guarded = _app()
    with TestClient(guarded, base_url=_BASE, client=("127.0.0.1", 49998)) as client:
        original = _login(client)
    def attempt(_):
        with TestClient(guarded, base_url=_BASE, client=("127.0.0.1", 49998)) as other:
            return other.post(
                "/api/ui/session/refresh",
                headers={**_ORIGIN, "Cookie": f"{COOKIE_NAME}={original}"},
            ).status_code
    with ThreadPoolExecutor(max_workers=10) as pool:
        outcomes = list(pool.map(attempt, range(25)))
    assert outcomes.count(204) == 1
    assert outcomes.count(401) == 24
    assert not missions_auth.validate_token(original)


def test_security_local_bootstrap_session_does_not_outlive_24_hours(monkeypatch):
    # Patch module time symbols, not time.monotonic globally (AnyIO needs it).
    clock = [1000.0]
    fake = SimpleNamespace(monotonic=lambda: clock[0])
    monkeypatch.setattr(surface_security, "time", fake)
    monkeypatch.setattr(missions_auth, "time", fake)
    secured = _app()
    secured._register_local_session("bootstrap-session-with-original-expiration")
    assert secured._issued_session_token_valid("bootstrap-session-with-original-expiration")
    # Delayed FastAPI import must preserve T+24h instead of resetting TTL.
    clock[0] += 1200
    secured.sync_local_tokens()
    assert missions_auth.validate_token("bootstrap-session-with-original-expiration")
    clock[0] = 1000.0 + surface_security._LOCAL_SESSION_TTL_S
    assert not secured._issued_session_token_valid("bootstrap-session-with-original-expiration")
    secured.sync_local_tokens()
    assert not missions_auth.validate_token("bootstrap-session-with-original-expiration")
    assert "bootstrap-session-with-original-expiration" not in secured._local_session_tokens


def test_security_mirrored_token_never_inherits_new_24h_lifetime(monkeypatch):
    clock = [500.0]
    fake = SimpleNamespace(monotonic=lambda: clock[0])
    monkeypatch.setattr(missions_auth, "time", fake)
    token = "legacy-issued-during-fast-boot"
    missions_auth.register_token(token, expires_at=clock[0] + 100)
    clock[0] += 90
    missions_auth.register_token(token, expires_at=clock[0] + 86400)
    assert missions_auth.validate_token(token)
    clock[0] += 10
    assert not missions_auth.validate_token(token)


def test_security_remote_plain_http_refresh_denied():
    public = "http://jarvis.example"
    guarded = SurfaceSecurity(
        FastAPI(), public_urls=(public,), control_key_validator=lambda key: key == _KEY
    )
    # An authenticated cookie is still not enough to refresh via plain HTTP.
    token = missions_auth.issue_token()
    with TestClient(guarded, base_url=public, client=("203.0.113.21", 49998)) as client:
        client.cookies.set(COOKIE_NAME, token)
        response = client.post("/api/ui/session/refresh", headers={"Origin": public})
        assert response.status_code == 403
        assert missions_auth.validate_token(token)


def test_function_https_refresh_and_logout_keep_secure_cookie():
    public = "https://jarvis.example"
    guarded = SurfaceSecurity(
        FastAPI(), public_urls=(public,), control_key_validator=lambda key: key == _KEY
    )
    with TestClient(guarded, base_url=public, client=("203.0.113.21", 49998)) as client:
        response = client.post("/api/ui/session", json={"control_key": _KEY}, headers={"Origin": public})
        assert response.status_code == 204
        renew = client.post("/api/ui/session/refresh", headers={"Origin": public})
        assert renew.status_code == 204
        assert "Secure" in renew.headers["set-cookie"]
        signout = client.post("/api/ui/session/logout", headers={"Origin": public})
        assert signout.status_code == 204
        assert "Secure" in signout.headers["set-cookie"]


def test_security_local_fast_boot_token_registry_is_bounded(monkeypatch):
    monkeypatch.setattr(surface_security, "_MAX_LOCAL_SESSION_TOKENS", 2)
    guarded = _app()
    guarded._register_local_session("first-cookie")
    guarded._register_local_session("second-cookie")
    guarded._register_local_session("third-cookie")
    assert len(guarded._local_session_tokens) == 2
    assert not guarded._issued_session_token_valid("first-cookie")
    assert guarded._issued_session_token_valid("second-cookie")
    assert guarded._issued_session_token_valid("third-cookie")
    assert not missions_auth.validate_token("first-cookie")

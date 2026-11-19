"""One-time mission socket credentials must not become HTTP sessions."""
from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jarvis.ui.web import missions_auth as auth
from jarvis.ui.web.missions_pty_routes import router as pty_router
from jarvis.ui.web.surface_security import COOKIE_NAME, SurfaceSecurity
from jarvis.ui.web import surface_security

pytestmark = pytest.mark.no_auto_web_auth
_BASE_URL = "http://127.0.0.1:47821"


@pytest.fixture(autouse=True)
def _clean_auth():
    auth.reset_tokens()
    surface_security.reset_browser_login_required()
    yield
    auth.reset_tokens()
    surface_security.reset_browser_login_required()


def test_ws_hello_token_is_single_use_and_not_a_session():
    token = auth.issue_ws_hello_token()
    assert isinstance(token, str) and len(token) >= 40
    assert not auth.validate_token(token)
    assert auth.validate_ws_hello_or_session(token)
    assert not auth.validate_ws_hello_or_session(token)
    assert not auth.validate_token(token)


def test_ws_hello_token_expires_after_one_minute(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(auth, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    token = auth.issue_ws_hello_token()
    clock[0] += 60
    assert not auth.consume_ws_hello_token(token)
    assert token not in auth._WS_HELLO_TOKENS


def test_ws_hello_store_is_bounded_without_evicting_live_tokens(monkeypatch):
    clock = [2000.0]
    monkeypatch.setattr(auth, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(auth, "_MAX_WS_HELLO_TOKENS", 2)
    first = auth.issue_ws_hello_token()
    second = auth.issue_ws_hello_token()
    assert auth.issue_ws_hello_token() is None
    assert auth.consume_ws_hello_token(first)
    assert auth.consume_ws_hello_token(second)
    assert auth.issue_ws_hello_token() is not None


def test_expired_ws_hello_tokens_are_pruned_before_capacity_check(monkeypatch):
    clock = [3000.0]
    monkeypatch.setattr(auth, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(auth, "_MAX_WS_HELLO_TOKENS", 1)
    first = auth.issue_ws_hello_token()
    assert auth.issue_ws_hello_token() is None
    clock[0] += 60
    second = auth.issue_ws_hello_token()
    assert second and second != first
    assert not auth.consume_ws_hello_token(first)
    assert auth.consume_ws_hello_token(second)


def test_concurrent_ws_hello_consumption_has_one_winner():
    token = auth.issue_ws_hello_token()
    with ThreadPoolExecutor(max_workers=24) as pool:
        outcomes = list(pool.map(auth.consume_ws_hello_token, [token] * 120))
    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 119


def test_existing_session_tokens_remain_valid_and_revocable():
    token = auth.issue_token()
    assert auth.validate_token(token)
    assert auth.validate_ws_hello_or_session(token)  # legacy desktop/API clients
    assert auth.validate_token(token)
    auth.revoke_token(token)
    assert not auth.validate_ws_hello_or_session(token)


def test_http_mint_saturation_returns_503_without_cache(monkeypatch):
    monkeypatch.setattr(auth, "_MAX_WS_HELLO_TOKENS", 1)
    response1 = asyncio.run(auth.get_token())
    response2 = asyncio.run(auth.get_token())
    assert response1.status_code == 200
    assert json.loads(response1.body)["expires_in"] == 60
    assert response2.status_code == 503
    assert response2.headers["cache-control"] == "no-store"
    assert response2.headers["retry-after"] == "1"
    assert "token" not in json.loads(response2.body)


def test_http_and_real_pty_websocket_one_time_auth():
    """Protected HTTP mint + route hello, then replay must be refused."""
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(pty_router)
    surface_security.set_browser_login_required(True)
    wrapped = SurfaceSecurity(app)
    with TestClient(wrapped, base_url=_BASE_URL, client=("127.0.0.1", 41000)) as client:
        unauth = client.get("/api/missions/auth/token")
        assert unauth.status_code == 401
        session = auth.issue_token()
        client.cookies.set(COOKIE_NAME, session)
        response = client.get("/api/missions/auth/token", headers={"Origin": _BASE_URL})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        token = response.json()["token"]
        assert not auth.validate_token(token)
        with pytest.raises(WebSocketDisconnect) as first:
            with client.websocket_connect("/api/missions/pty/test-worker", headers={"Origin": _BASE_URL, "Host": "127.0.0.1:47821"}) as ws:
                ws.send_json({"type": "hello", "token": token})
                ws.receive_json()
        assert first.value.code == 4404  # Auth succeeded; unknown worker.
        with pytest.raises(WebSocketDisconnect) as second:
            with client.websocket_connect("/api/missions/pty/test-worker", headers={"Origin": _BASE_URL, "Host": "127.0.0.1:47821"}) as ws:
                ws.send_json({"type": "hello", "token": token})
                ws.receive_json()
        assert second.value.code == 4401  # Replay rejected.
        assert auth.validate_token(session)  # The real session was not consumed.


def test_ws_hello_token_does_not_authenticate_general_http():
    """A stolen browser hello credential cannot be reused as an HTTP Bearer."""
    app = FastAPI()
    app.include_router(auth.router)
    surface_security.set_browser_login_required(True)
    with TestClient(SurfaceSecurity(app), base_url=_BASE_URL, client=("127.0.0.1", 41000)) as client:
        token = auth.issue_ws_hello_token()
        response = client.get("/api/missions/auth/token", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401
        assert auth.consume_ws_hello_token(token)  # HTTP denial did not consume it.


def test_http_mint_and_mission_event_websocket_end_to_end():
    """A one-time token authenticates the actual mission event WS route."""
    from types import SimpleNamespace
    from jarvis.ui.web.missions_ws_routes import router as missions_ws_router

    class FakeConnectionManager:
        def __init__(self):
            self.connected = False

        async def connect(self, client_id, last_seq, store):
            self.connected = True
            queue = asyncio.Queue()
            queue.put_nowait({"type": "test_event", "seq": last_seq + 1})
            return queue

        async def disconnect(self, client_id):
            self.connected = False

    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(missions_ws_router)
    app.state.missions_ws_manager = FakeConnectionManager()
    app.state.mission_manager = SimpleNamespace(store=object())
    surface_security.set_browser_login_required(True)
    with TestClient(SurfaceSecurity(app), base_url=_BASE_URL, client=("127.0.0.1", 41000)) as client:
        client.cookies.set(COOKIE_NAME, auth.issue_token())
        token = client.get("/api/missions/auth/token", headers={"Origin": _BASE_URL}).json()["token"]
        headers = {"Origin": _BASE_URL, "Host": "127.0.0.1:47821"}
        with client.websocket_connect("/api/missions/ws", headers=headers) as ws:
            ws.send_json({"type": "hello", "last_seq": 5, "token": token})
            assert ws.receive_json() == {"type": "test_event", "seq": 6}
        with pytest.raises(WebSocketDisconnect) as reused:
            with client.websocket_connect("/api/missions/ws", headers=headers) as ws:
                ws.send_json({"type": "hello", "last_seq": 5, "token": token})
                ws.receive_json()
        assert reused.value.code == 4401


def test_cross_origin_socket_is_rejected_before_hello_token_is_used():
    """The global Origin guard remains mandatory even for a valid hello token."""
    app = FastAPI()
    app.include_router(pty_router)
    surface_security.set_browser_login_required(True)
    token = auth.issue_ws_hello_token()
    with TestClient(SurfaceSecurity(app), base_url=_BASE_URL, client=("127.0.0.1", 41000)) as client:
        with pytest.raises(WebSocketDisconnect) as foreign:
            with client.websocket_connect(
                "/api/missions/pty/worker", headers={"Origin": "https://attacker.invalid", "Host": "127.0.0.1:47821"}
            ) as ws:
                ws.send_json({"type": "hello", "token": token})
                ws.receive_json()
        assert foreign.value.code == 4403
        with pytest.raises(WebSocketDisconnect) as same_origin:
            with client.websocket_connect(
                "/api/missions/pty/worker", headers={"Origin": _BASE_URL, "Host": "127.0.0.1:47821"}
            ) as ws:
                ws.send_json({"type": "hello", "token": token})
                ws.receive_json()
        assert same_origin.value.code == 4404

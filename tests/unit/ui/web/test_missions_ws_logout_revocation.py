"""Session-owned mission tickets and live sockets must end at logout/refresh."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jarvis.ui.web import missions_auth as auth, surface_security
from jarvis.ui.web.missions_pty_routes import router as pty_router
from jarvis.ui.web.missions_ws_routes import router as events_router
from jarvis.ui.web.surface_security import COOKIE_NAME, SurfaceSecurity

pytestmark = pytest.mark.no_auto_web_auth
_BASE = "http://127.0.0.1:47821"
_HEADERS = {"Origin": _BASE, "Host": "127.0.0.1:47821"}
_KEY = "test-control-key"


@pytest.fixture(autouse=True)
def reset():
    auth.reset_tokens()
    surface_security.set_browser_login_required(True)
    yield
    auth.reset_tokens()
    surface_security.reset_browser_login_required()


class FakeManager:
    def __init__(self):
        self.connected = False

    async def connect(self, client_id, last_seq, store):
        self.connected = True
        q = asyncio.Queue()
        q.put_nowait({"type": "initial", "seq": last_seq + 1})
        return q

    async def disconnect(self, client_id):
        self.connected = False


def make_app(*, worker_log=None):
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(events_router)
    app.include_router(pty_router)
    app.state.missions_ws_manager = FakeManager()
    app.state.mission_manager = SimpleNamespace(store=object())
    if worker_log is not None:
        app.state.mission_worker_logs = {"test-worker": worker_log}
    return SurfaceSecurity(app, control_key_validator=lambda key: key == _KEY)


def client_for(app):
    return TestClient(app, base_url=_BASE, client=("127.0.0.1", 49998))


def login(client):
    response = client.post("/api/ui/session", json={"control_key": _KEY}, headers=_HEADERS)
    assert response.status_code == 204
    return client.cookies.get(COOKIE_NAME)


def mint(client):
    response = client.get("/api/missions/auth/token", headers=_HEADERS)
    assert response.status_code == 200, response.text
    return response.json()["token"]


def test_function_ticket_is_bound_to_cookie_and_logout_revokes_unused():
    with client_for(make_app()) as client:
        session = login(client)
        ticket = mint(client)
        assert ticket in auth._WS_HELLO_TOKENS
        assert auth._WS_HELLO_TOKENS[ticket][1] == session
        assert client.post("/api/ui/session/logout", headers=_HEADERS).status_code == 204
        assert ticket not in auth._WS_HELLO_TOKENS
        assert not auth.consume_ws_hello_token(ticket)
        assert not auth.validate_token(session)


def test_function_refresh_invalidates_old_tickets_but_allows_new_tickets():
    with client_for(make_app()) as client:
        original = login(client)
        old_ticket = mint(client)
        assert client.post("/api/ui/session/refresh", headers=_HEADERS).status_code == 204
        assert not auth.consume_ws_hello_token(old_ticket)
        new_ticket = mint(client)
        assert auth.consume_ws_hello_token(new_ticket)
        assert not auth.validate_token(original)


def test_function_other_session_tickets_are_not_revoked():
    guarded = make_app()
    with client_for(guarded) as first, client_for(guarded) as second:
        login(first)
        login(second)
        first_ticket, second_ticket = mint(first), mint(second)
        assert first.post("/api/ui/session/logout", headers=_HEADERS).status_code == 204
        assert not auth.consume_ws_hello_token(first_ticket)
        assert auth.consume_ws_hello_token(second_ticket)


def test_security_owner_expiry_rejects_even_if_ticket_was_not_pruned():
    session = auth.issue_token()
    ticket = auth.issue_ws_hello_token(session_token=session)
    assert ticket
    # Simulates a session expiring/being removed independently of ticket store.
    with auth._LOCK:
        auth._TOKENS.pop(session)
    assert not auth.consume_ws_hello_token(ticket)


def test_security_invalid_owner_never_mints_ticket():
    assert auth.issue_ws_hello_token(session_token="forged-session") is None


def test_security_stolen_ticket_cannot_outlive_session_or_be_http_cookie():
    with client_for(make_app()) as client:
        login(client)
        ticket = mint(client)
        assert not auth.validate_token(ticket)
        assert client.post("/api/ui/session/logout", headers=_HEADERS).status_code == 204
        with pytest.raises(WebSocketDisconnect) as refused:
            with client.websocket_connect("/api/missions/ws", headers=_HEADERS) as ws:
                ws.send_json({"type": "hello", "token": ticket, "last_seq": 0})
                ws.receive_json()
        assert refused.value.code == 4401


def test_integration_active_mission_socket_closes_after_logout():
    guarded = make_app()
    with client_for(guarded) as client:
        login(client)
        ticket = mint(client)
        with client.websocket_connect("/api/missions/ws", headers=_HEADERS) as ws:
            ws.send_json({"type": "hello", "token": ticket, "last_seq": 0})
            assert ws.receive_json() == {"type": "initial", "seq": 1}
            assert client.post("/api/ui/session/logout", headers=_HEADERS).status_code == 204
            with pytest.raises(WebSocketDisconnect) as ended:
                ws.receive_json()
            assert ended.value.code == 4401
        assert not guarded.app.state.missions_ws_manager.connected


def test_integration_active_pty_socket_closes_after_logout(tmp_path):
    logfile = tmp_path / "worker.log"
    logfile.write_text("hello")
    with client_for(make_app(worker_log=logfile)) as client:
        login(client)
        ticket = mint(client)
        with client.websocket_connect("/api/missions/pty/test-worker", headers=_HEADERS) as ws:
            ws.send_json({"type": "hello", "token": ticket})
            assert ws.receive_json()["type"] == "info"
            assert client.post("/api/ui/session/logout", headers=_HEADERS).status_code == 204
            with pytest.raises(WebSocketDisconnect) as ended:
                ws.receive_json()
            assert ended.value.code == 4401


def test_integration_legacy_session_socket_also_ends_on_refresh():
    with client_for(make_app()) as client:
        session = login(client)
        with client.websocket_connect("/api/missions/ws", headers=_HEADERS) as ws:
            ws.send_json({"type": "hello", "token": session, "last_seq": 0})
            assert ws.receive_json()["type"] == "initial"
            assert client.post("/api/ui/session/refresh", headers=_HEADERS).status_code == 204
            with pytest.raises(WebSocketDisconnect) as ended:
                ws.receive_json()
            assert ended.value.code == 4401


def test_regression_control_key_tickets_without_cookie_remain_available():
    with client_for(make_app()) as client:
        response = client.get("/api/missions/auth/token", headers={**_HEADERS, "Authorization": f"Bearer {_KEY}"})
        assert response.status_code == 200
        ticket = response.json()["token"]
        assert auth.consume_ws_hello_token(ticket)


def test_security_cross_origin_logout_does_not_revoke_ticket():
    with client_for(make_app()) as client:
        login(client)
        ticket = mint(client)
        assert client.post("/api/ui/session/logout", headers={"Origin": "https://evil.invalid"}).status_code == 403
        assert auth.consume_ws_hello_token(ticket)

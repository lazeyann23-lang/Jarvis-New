"""Process-local authentication tokens for UI sessions and mission sockets.

Session cookies have a 24-hour lifetime. Mission WebSocket hello credentials
are deliberately separate: they expire after 60 seconds, can be used only
once, and cannot authenticate HTTP requests or other application routes.
The HTTP minting endpoint must remain behind the global Host/Origin/session
security boundary.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Final

from jarvis.core.branding import SESSION_COOKIE_NAME
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

# Fixed 24-hour lifetime (monotonic clock resists wall-clock changes). Tokens
# are not refreshed by validation; a stolen token cannot extend its lifetime.
_TOKEN_TTL_SECONDS: Final[int] = 24 * 60 * 60
_MAX_TOKENS: Final[int] = 4096
_TOKEN_BYTES: Final[int] = 32
_TOKENS: dict[str, float] = {}  # token -> monotonic expiration
_LOCK = threading.Lock()

# WebSocket hello credentials are NOT general-purpose session tokens.
# A reconnect obtains a new token; neither the URL nor browser storage holds it.
_WS_HELLO_TTL_SECONDS: Final[int] = 60
_MAX_WS_HELLO_TOKENS: Final[int] = 256
_WS_HELLO_TOKENS: dict[str, tuple[float, str | None]] = {}  # ticket -> (expiry, issuing session)
_WS_HELLO_LOCK = threading.Lock()


def _prune_expired(now: float) -> None:
    """Remove expired entries. Caller must hold _LOCK."""
    for token, expires_at in list(_TOKENS.items()):
        if expires_at <= now:
            del _TOKENS[token]


def _store_token(token: str, *, expires_at: float | None = None) -> None:
    """Register without extending a session's original monotonic expiry."""
    now = time.monotonic()
    with _LOCK:
        _prune_expired(now)
        if expires_at is not None and expires_at <= now:
            return
        if token in _TOKENS:
            # Re-registering a valid session must not extend its lifetime.
            return
        if len(_TOKENS) >= _MAX_TOKENS:
            # Evict the token that expires soonest.
            del _TOKENS[min(_TOKENS, key=_TOKENS.get)]
        _TOKENS[token] = min(
            now + _TOKEN_TTL_SECONDS, expires_at
        ) if expires_at is not None else now + _TOKEN_TTL_SECONDS


def issue_token() -> str:
    """Generate a cryptographically random URL-safe token with 24h lifetime."""
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    _store_token(token)
    return token


def validate_token(tok: str) -> bool:
    """Return whether a token is registered and has not expired."""
    if not tok:
        return False
    now = time.monotonic()
    with _LOCK:
        expiry = _TOKENS.get(tok)
        if expiry is None:
            return False
        if expiry <= now:
            _TOKENS.pop(tok, None)
            return False
        return True


@dataclass(frozen=True)
class SocketAuthorization:
    """An accepted socket credential and its revocable browser session.

    ``session_token=None`` is reserved for a ticket minted by an explicitly
    authorized non-session caller (e.g. local open-access or Control API key).
    """

    session_token: str | None


def issue_ws_hello_token(*, session_token: str | None = None) -> str | None:
    """Mint a one-use 60s ticket tied to the issuing UI session, if any.

    When the user signs out or refreshes their session, their unused tickets
    become invalid too. Legacy authenticated control-key/open-access clients
    can still request tickets without a browser session.
    """
    if session_token is not None and not validate_token(session_token):
        return None
    now = time.monotonic()
    with _WS_HELLO_LOCK:
        for token, (expiry, _) in list(_WS_HELLO_TOKENS.items()):
            if expiry <= now:
                del _WS_HELLO_TOKENS[token]
        if len(_WS_HELLO_TOKENS) >= _MAX_WS_HELLO_TOKENS:
            return None
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        _WS_HELLO_TOKENS[token] = (now + _WS_HELLO_TTL_SECONDS, session_token)
        return token


def _consume_ws_authorization(tok: str) -> SocketAuthorization | None:
    """Atomically consume a ticket, then check its original session."""
    if not tok:
        return None
    now = time.monotonic()
    with _WS_HELLO_LOCK:
        record = _WS_HELLO_TOKENS.pop(tok, None)
    if record is None:
        return None
    expiry, owner = record
    if expiry <= now or (owner is not None and not validate_token(owner)):
        return None
    return SocketAuthorization(session_token=owner)


def consume_ws_hello_token(tok: str) -> bool:
    """Atomically validate and consume a single-use WebSocket credential."""
    return _consume_ws_authorization(tok) is not None


def authorize_ws_hello_or_session(tok: str) -> SocketAuthorization | None:
    """Authorize a one-use ticket or a legacy UI session credential.

    The returned session token lets active mission sockets notice logout,
    refresh, or expiry instead of staying authorized until TCP disconnect.
    """
    ticket = _consume_ws_authorization(tok)
    if ticket is not None:
        return ticket
    if validate_token(tok):
        return SocketAuthorization(session_token=tok)
    return None


def validate_ws_hello_or_session(tok: str) -> bool:
    """Compatibility boolean API for older clients and existing tests."""
    return authorize_ws_hello_or_session(tok) is not None


def register_token(tok: str, *, expires_at: float | None = None) -> None:
    """Mirror an already-issued token, preserving its absolute expiration.

    Tokens minted by fast boot must not receive another 24-hour lifetime
    merely because the main app imported the auth module later.
    """
    if tok:
        _store_token(tok, expires_at=expires_at)


def revoke_token(tok: str) -> None:
    """Invalidate a token immediately; safe to call repeatedly."""
    with _LOCK:
        _TOKENS.pop(tok, None)
    # Remove unused tickets minted by this browser session. Tickets already
    # consumed by an active socket are checked by that socket's session guard.
    with _WS_HELLO_LOCK:
        for ticket, (_, owner) in list(_WS_HELLO_TOKENS.items()):
            if owner == tok:
                del _WS_HELLO_TOKENS[ticket]


def reset_tokens() -> None:
    """Test hook: clear all registered tokens."""
    with _LOCK:
        _TOKENS.clear()
    with _WS_HELLO_LOCK:
        _WS_HELLO_TOKENS.clear()


router = APIRouter(prefix="/api/missions/auth", tags=["missions-auth"])


@router.get("/token")
async def get_token(request: Request = None) -> JSONResponse:
    """Issue a single-use ticket bound to the authenticated browser cookie.

    The global SurfaceSecurity middleware authorizes this endpoint. Requests
    using a Control key or permitted local open-access have no cookie owner.
    """
    session = request.cookies.get(SESSION_COOKIE_NAME) if request is not None else None
    if session and not validate_token(session):
        return JSONResponse({"detail": "Invalid UI session."}, status_code=401,
                            headers={"Cache-Control": "no-store"})
    token = issue_ws_hello_token(session_token=session)
    if token is None:
        return JSONResponse(
            {"detail": "Too many pending WebSocket connections; retry shortly."},
            status_code=503,
            headers={"Cache-Control": "no-store", "Retry-After": "1"},
        )
    return JSONResponse(
        {"token": token, "expires_in": _WS_HELLO_TTL_SECONDS},
        headers={"Cache-Control": "no-store"},
    )

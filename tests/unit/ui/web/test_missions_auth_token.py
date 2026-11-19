"""Unit tests for process-local authenticated session tokens."""
from __future__ import annotations

from jarvis.ui.web.missions_auth import (
    issue_token,
    register_token,
    reset_tokens,
    revoke_token,
    validate_token,
)


def test_raw_unissued_token_is_rejected_until_registered() -> None:
    reset_tokens()
    raw = "raw-session-token-never-issued-0123456789"
    assert validate_token(raw) is False

    register_token(raw)
    assert validate_token(raw) is True


def test_revoke_token_invalidates_registered_token() -> None:
    reset_tokens()
    raw = "registered-session-that-will-be-revoked"
    register_token(raw)
    revoke_token(raw)
    assert validate_token(raw) is False


def test_issued_tokens_still_validate() -> None:
    reset_tokens()
    tok = issue_token()
    assert validate_token(tok) is True


# --- Session hardening (2026-10-21) ---


def test_expired_token_is_rejected_and_removed(monkeypatch) -> None:
    from jarvis.ui.web import missions_auth as auth

    reset_tokens()
    clock = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock[0])
    token = issue_token()
    assert validate_token(token)
    clock[0] += auth._TOKEN_TTL_SECONDS
    assert not validate_token(token)
    assert token not in auth._TOKENS


def test_validation_does_not_extend_token_lifetime(monkeypatch) -> None:
    from jarvis.ui.web import missions_auth as auth

    reset_tokens()
    clock = [2000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock[0])
    token = issue_token()
    clock[0] += auth._TOKEN_TTL_SECONDS - 1
    assert validate_token(token)
    clock[0] += 1
    assert not validate_token(token)


def test_bounded_store_evicts_oldest_expiry(monkeypatch) -> None:
    from jarvis.ui.web import missions_auth as auth

    reset_tokens()
    monkeypatch.setattr(auth, "_MAX_TOKENS", 2)
    clock = [3000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock[0])
    first = issue_token()
    clock[0] += 1
    second = issue_token()
    clock[0] += 1
    third = issue_token()
    assert not validate_token(first)
    assert validate_token(second) and validate_token(third)
    assert len(auth._TOKENS) == 2


def test_revocation_still_works_after_store_eviction(monkeypatch) -> None:
    from jarvis.ui.web import missions_auth as auth

    reset_tokens()
    monkeypatch.setattr(auth, "_MAX_TOKENS", 2)
    first, second, third = issue_token(), issue_token(), issue_token()
    assert not validate_token(first)
    revoke_token(third)
    assert not validate_token(third)
    assert validate_token(second)


def test_get_token_route_is_uncacheable_and_issues_valid_token() -> None:
    import asyncio
    import json
    from jarvis.ui.web import missions_auth as auth
    from jarvis.ui.web.missions_auth import get_token

    reset_tokens()
    response = asyncio.run(get_token())
    token = json.loads(response.body)["token"]
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert auth.consume_ws_hello_token(token)
    assert not auth.consume_ws_hello_token(token)
    assert not validate_token(token)


def test_re_registration_does_not_extend_lifetime(monkeypatch) -> None:
    from jarvis.ui.web import missions_auth as auth

    reset_tokens()
    clock = [4000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock[0])
    token = issue_token()
    clock[0] += auth._TOKEN_TTL_SECONDS - 1
    register_token(token)
    clock[0] += 1
    assert not validate_token(token)


def test_token_randomness_and_format() -> None:
    reset_tokens()
    tokens = {issue_token() for _ in range(100)}
    assert len(tokens) == 100
    assert all(len(token) >= 40 for token in tokens)
    assert all(validate_token(token) for token in tokens)

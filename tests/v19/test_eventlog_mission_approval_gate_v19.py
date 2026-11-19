"""V19: durable event-log barrier for mission tool approvals.

Executes the production MissionEventStore SQL against real stdlib SQLite
using a minimal async cursor adapter. No production aiosqlite installation,
real OS workers, or external tools are involved.
"""
from __future__ import annotations

import sqlite3
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

try:
    import aiosqlite  # noqa: F401
except ImportError:
    shim = types.ModuleType("aiosqlite")
    for name in ("Connection", "Cursor", "Row"):
        setattr(shim, name, type(name, (), {}))
    sys.modules["aiosqlite"] = shim

from jarvis.core.bus import EventBus
from jarvis.core.events import ActionApprovalRequired, ActionApproved, ActionDenied
from jarvis.missions.event_store import MissionEventStore, _SCHEMA_PATH
from jarvis.missions.manager import MissionManager
from jarvis.missions.state_machine import MissionState
from jarvis.missions.tool_approvals import MissionToolApprovalCoordinator, MissionToolAutoApprover
from jarvis.ui.web.server import WebServer


class AsyncCursor:
    def __init__(self, cursor):
        self.cursor = cursor

    async def fetchall(self):
        return self.cursor.fetchall()

    async def close(self):
        self.cursor.close()


class AsyncSQLite:
    def __init__(self, connection):
        self.connection = connection

    async def execute(self, query, params=()):
        return AsyncCursor(self.connection.execute(query, params))


def setup_db(path: Path):
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def header(conn, mission="m1", state="RUNNING"):
    conn.execute(
        """INSERT INTO missions (id, prompt, state, language, created_ms, updated_ms)
        VALUES (?, 'task', ?, 'de', 1, 1)
        ON CONFLICT(id) DO UPDATE SET state=excluded.state""",
        (mission, state),
    )


def event(conn, mission="m1", to_state="RUNNING", *, event_type="MissionStateChanged", payload=None):
    if payload is None:
        payload = '{"to_state":"' + to_state + '"}'
    conn.execute(
        """INSERT INTO mission_events
        (event_id, mission_id, event_type, source_actor, ts_ms, payload_json)
        VALUES (?, ?, ?, 'ui', 1, ?)""",
        (str(uuid4()), mission, event_type, payload),
    )


def store(conn):
    result = MissionEventStore(Path("/unused"), bus=None)
    result._conn = AsyncSQLite(conn)
    return result


def manager(conn):
    result = MissionManager(Path("/unused"))
    result._store = store(conn)
    result._started = True
    return result


def request(mission="m1"):
    return ActionApprovalRequired(
        trace_id=uuid4(), mission_id=mission, tool_name="external-action",
        risk_tier="ask", reason="test", args_preview="{}",
        expires_at_ns=time.time_ns() + 30_000_000_000,
    )


# FUNCTION: production SQL and state interpretation.
@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["RUNNING", "CRITIQUING", "LOOPING"])
async def test_active_header_and_eventlog_allow_approval(tmp_path, state):
    conn = setup_db(tmp_path / "missions.db")
    try:
        header(conn, state=state)
        event(conn, to_state=state)
        assert await store(conn).is_active_for_tool_approval("m1")
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_header_active_but_cancel_transition_persisted_denied(tmp_path):
    conn = setup_db(tmp_path / "missions.db")
    try:
        header(conn)
        event(conn, to_state="RUNNING")
        event(conn, to_state="CANCELLED")  # crash before header upsert
        assert not await store(conn).is_active_for_tool_approval("m1")
        assert conn.execute("SELECT state FROM missions WHERE id='m1'").fetchone()[0] == "RUNNING"
    finally:
        conn.close()


# INTEGRATION: production Manager/WebServer state reader and actual bus events.
@pytest.mark.asyncio
async def test_production_reader_rejects_crash_gap_auto_approval(tmp_path):
    conn = setup_db(tmp_path / "missions.db")
    try:
        header(conn)
        event(conn, to_state="RUNNING")
        event(conn, to_state="CANCELLED")
        mgr = manager(conn)
        fake_server = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(mission_manager=mgr)))
        async def reader(mission_id):
            return await WebServer._mission_is_active_for_tool_approval(fake_server, mission_id)
        assert not await reader("m1")
        bus = EventBus()
        coordinator = MissionToolApprovalCoordinator(bus, mission_is_active=reader)
        auto = MissionToolAutoApprover(bus, coordinator=coordinator)
        auto.arm("m1", "grant", frozenset({"external-action"}))
        approvals, denials = [], []
        async def on_approved(ev): approvals.append(ev)
        async def on_denied(ev): denials.append(ev)
        bus.subscribe(ActionApproved, on_approved)
        bus.subscribe(ActionDenied, on_denied)
        req = request()
        await bus.publish(req)
        assert approvals == []
        assert [x.trace_id for x in denials] == [req.trace_id]
        assert await coordinator.list_pending("m1") == ()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_other_connection_appends_terminal_event_without_header_update(tmp_path):
    db = tmp_path / "missions.db"
    conn = setup_db(db)
    other = sqlite3.connect(db, isolation_level=None)
    try:
        header(conn)
        event(conn, to_state="RUNNING")
        mgr = manager(conn)
        assert await mgr.is_active_for_tool_approval("m1")
        event(other, to_state="FAILED")  # second connection commits event only
        assert not await mgr.is_active_for_tool_approval("m1")
        assert conn.execute("SELECT state FROM missions WHERE id='m1'").fetchone()[0] == "RUNNING"
    finally:
        other.close()
        conn.close()


# SECURITY/REGRESSION: malformed, missing and historical terminal events.
@pytest.mark.asyncio
async def test_terminal_marker_without_header_update_denied(tmp_path):
    conn = setup_db(tmp_path / "missions.db")
    try:
        header(conn)
        event(conn, to_state="RUNNING")
        event(conn, event_type="MissionCancelled", payload='{"reason":"ui_cancel"}')
        assert not await store(conn).is_active_for_tool_approval("m1")
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_terminal_event_is_sticky_even_if_later_active_event(tmp_path):
    conn = setup_db(tmp_path / "missions.db")
    try:
        header(conn)
        event(conn, to_state="RUNNING")
        event(conn, to_state="CANCELLED")
        event(conn, to_state="RUNNING")  # illegal resurrection must not grant
        assert not await store(conn).is_active_for_tool_approval("m1")
    finally:
        conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["not-json", '{"to_state":null}', '{"unexpected":"x"}', '{"to_state":"UNKNOWN"}'])
async def test_malformed_lifecycle_event_fails_closed(tmp_path, bad):
    conn = setup_db(tmp_path / "missions.db")
    try:
        header(conn)
        event(conn, payload=bad)
        assert not await store(conn).is_active_for_tool_approval("m1")
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_other_mission_unaffected_and_no_event_denied(tmp_path):
    conn = setup_db(tmp_path / "missions.db")
    try:
        header(conn, "m1")
        event(conn, "m1", "CANCELLED")
        header(conn, "m2")
        event(conn, "m2", "RUNNING")
        header(conn, "m3")  # no transition event
        checker = store(conn)
        assert not await checker.is_active_for_tool_approval("m1")
        assert await checker.is_active_for_tool_approval("m2")
        assert not await checker.is_active_for_tool_approval("m3")
        assert not await checker.is_active_for_tool_approval("unknown")
    finally:
        conn.close()

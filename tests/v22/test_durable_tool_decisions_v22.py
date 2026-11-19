"""V22 durable first-writer-wins tool decisions (no real tool execution).

Runs production SQLite statements through a small stdlib adapter because
aiosqlite is not installed in this environment. Tests do not claim to exercise
the production async SQLite driver or cross-store cancel/publish atomicity.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import threading
import time
import types
from pathlib import Path
from uuid import UUID, uuid4

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
from jarvis.missions.tool_approvals import MissionToolApprovalCoordinator, MissionToolAutoApprover


class AsyncCursor:
    def __init__(self, cursor):
        self.cursor = cursor

    async def fetchone(self):
        return self.cursor.fetchone()

    async def fetchall(self):
        return self.cursor.fetchall()

    async def close(self):
        self.cursor.close()


class AsyncSQLite:
    def __init__(self, connection):
        self.connection = connection

    async def execute(self, query, params=()):
        return AsyncCursor(self.connection.execute(query, params))


def database(path: Path):
    conn = sqlite3.connect(path, isolation_level=None, timeout=5)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def add_mission(conn, mission="m1", state="RUNNING"):
    conn.execute(
        "INSERT INTO missions (id,prompt,state,language,created_ms,updated_ms) "
        "VALUES (?, 'task', ?, 'de', 1, 1)", (mission, state)
    )
    add_event(conn, mission, state)


def add_event(conn, mission="m1", state="RUNNING", event_type="MissionStateChanged", payload=None):
    if payload is None:
        payload = '{"to_state":"' + state + '"}'
    conn.execute(
        "INSERT INTO mission_events "
        "(event_id,mission_id,event_type,source_actor,ts_ms,payload_json) "
        "VALUES (?,?,?,'ui',1,?)", (str(uuid4()), mission, event_type, payload)
    )


def store(conn):
    obj = MissionEventStore(Path('/unused'), bus=None)
    obj._conn = AsyncSQLite(conn)
    return obj


async def claim(s, trace=None, mission="m1", tool="external-action", decision="auto_approved"):
    return await s.try_claim_tool_decision(
        trace_id=str(trace or uuid4()), mission_id=mission,
        tool_name=tool, decision=decision,
    )


def request(trace=None, mission="m1", tool="external-action"):
    return ActionApprovalRequired(
        trace_id=trace or uuid4(), mission_id=mission, worker_id="worker",
        tool_name=tool, risk_tier="ask", reason="risk_tier",
        args_preview="<redacted>",
        expires_at_ns=time.time_ns() + 30_000_000_000,
    )


def coordinator(bus, s):
    async def durable(trace: UUID, mission: str, tool: str, decision: str):
        return await claim(s, trace, mission, tool, decision)
    return MissionToolApprovalCoordinator(
        bus, mission_is_active=s.is_active_for_tool_approval,
        durable_claim=durable,
    )


# FUNCTION TESTS
@pytest.mark.asyncio
async def test_function_durable_claim_survives_reopen_and_rejects_duplicate(tmp_path):
    path = tmp_path / "mission.db"
    conn = database(path)
    try:
        add_mission(conn)
        trace = uuid4()
        assert await claim(store(conn), trace)
        assert not await claim(store(conn), trace)
        assert conn.execute("SELECT COUNT(*) FROM mission_tool_approval_decisions").fetchone()[0] == 1
    finally:
        conn.close()
    reopened = database(path)
    try:
        assert not await claim(store(reopened), trace)
        assert await claim(store(reopened), uuid4())
    finally:
        reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("state,allowed", [
    ("RUNNING", True), ("CRITIQUING", True), ("LOOPING", True),
    ("WAITING_CAPACITY", False), ("PENDING", False),
])
async def test_function_only_active_states_can_claim(tmp_path, state, allowed):
    conn = database(tmp_path / "mission.db")
    try:
        add_mission(conn, state=state)
        assert await claim(store(conn)) is allowed
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_function_invalid_decision_never_writes(tmp_path):
    conn = database(tmp_path / "mission.db")
    try:
        add_mission(conn)
        assert not await claim(store(conn), decision="block")
        assert conn.execute("SELECT COUNT(*) FROM mission_tool_approval_decisions").fetchone()[0] == 0
    finally:
        conn.close()


# INTEGRATION TESTS
@pytest.mark.asyncio
async def test_integration_two_coordinators_one_autoapproval(tmp_path):
    path = tmp_path / "mission.db"
    first, second = database(path), database(path)
    try:
        add_mission(first)
        approvals = []
        buses = [EventBus(), EventBus()]
        for bus, conn in zip(buses, (first, second)):
            coord = coordinator(bus, store(conn))
            auto = MissionToolAutoApprover(bus, coordinator=coord)
            auto.arm("m1", "grant", frozenset({"external-action"}))
            async def observe(ev):
                approvals.append(ev)
            bus.subscribe(ActionApproved, observe)
        event = request()
        await asyncio.gather(*(bus.publish(event) for bus in buses))
        assert len(approvals) == 1
        assert approvals[0].trace_id == event.trace_id
    finally:
        second.close()
        first.close()


@pytest.mark.asyncio
async def test_integration_manual_denial_prevents_later_autoapproval_after_restart(tmp_path):
    path = tmp_path / "mission.db"
    first = database(path)
    try:
        add_mission(first)
        bus = EventBus()
        coord = coordinator(bus, store(first))
        event = request()
        await bus.publish(event)
        assert await coord.deny("m1", event.trace_id, reason="user_denied") is not None
        assert first.execute("SELECT decision FROM mission_tool_approval_decisions").fetchone()[0] == "denied"
    finally:
        first.close()
    second = database(path)
    try:
        bus = EventBus()
        coord = coordinator(bus, store(second))
        auto = MissionToolAutoApprover(bus, coordinator=coord)
        auto.arm("m1", "grant", frozenset({"external-action"}))
        approvals = []
        async def observe(ev): approvals.append(ev)
        bus.subscribe(ActionApproved, observe)
        await bus.publish(event)
        assert approvals == []
    finally:
        second.close()


def test_integration_two_sqlite_connections_race_first_writer_wins(tmp_path):
    path = tmp_path / "mission.db"
    initial = database(path)
    add_mission(initial)
    initial.close()
    barrier = threading.Barrier(2)
    trace = uuid4()
    results = []
    errors = []
    def worker():
        try:
            conn = database(path)
            try:
                barrier.wait(timeout=3)
                results.append(asyncio.run(claim(store(conn), trace)))
            finally:
                conn.close()
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=6)
    assert not errors
    assert sorted(results) == [False, True]


@pytest.mark.asyncio
async def test_integration_webserver_callback_uses_real_store(tmp_path):
    from types import SimpleNamespace
    from jarvis.ui.web.server import WebServer
    conn = database(tmp_path / "mission.db")
    try:
        add_mission(conn)
        fake_server = SimpleNamespace(app=SimpleNamespace(
            state=SimpleNamespace(mission_manager=SimpleNamespace(store=store(conn)))
        ))
        trace = uuid4()
        claim_from_server = WebServer._claim_persisted_mission_tool_decision
        assert await claim_from_server(fake_server, trace, "m1", "external-action", "approved")
        assert not await claim_from_server(fake_server, trace, "m1", "external-action", "approved")
        fake_server.app.state.mission_manager = None
        assert not await claim_from_server(fake_server, uuid4(), "m1", "external-action", "approved")
    finally:
        conn.close()


# SECURITY/REGRESSION TESTS
@pytest.mark.asyncio
async def test_security_stale_running_header_with_cancel_event_denies(tmp_path):
    conn = database(tmp_path / "mission.db")
    try:
        add_mission(conn)
        add_event(conn, state="CANCELLED")
        assert not await claim(store(conn))
        assert conn.execute("SELECT COUNT(*) FROM mission_tool_approval_decisions").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_security_terminal_event_sticky_even_if_later_running(tmp_path):
    conn = database(tmp_path / "mission.db")
    try:
        add_mission(conn)
        add_event(conn, state="FAILED")
        add_event(conn, state="RUNNING")
        assert not await claim(store(conn))
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_security_corrupt_event_json_fails_closed(tmp_path):
    conn = database(tmp_path / "mission.db")
    try:
        add_mission(conn)
        add_event(conn, payload="{not valid json")
        assert not await claim(store(conn))
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_security_database_failure_never_publishes_approval():
    bus = EventBus()
    async def active(_mission): return True
    async def fail_claim(*_args): raise OSError("database unavailable")
    coord = MissionToolApprovalCoordinator(
        bus, mission_is_active=active, durable_claim=fail_claim,
    )
    auto = MissionToolAutoApprover(bus, coordinator=coord)
    auto.arm("m1", "grant", frozenset({"external-action"}))
    approvals = []
    async def observe(ev): approvals.append(ev)
    bus.subscribe(ActionApproved, observe)
    await bus.publish(request())
    assert approvals == []


@pytest.mark.asyncio
async def test_security_trace_collision_across_missions_and_tools(tmp_path):
    conn = database(tmp_path / "mission.db")
    try:
        add_mission(conn, "m1")
        add_mission(conn, "m2")
        trace = uuid4()
        assert await claim(store(conn), trace, "m1", "external-action")
        assert not await claim(store(conn), trace, "m2", "other-tool")
        assert conn.execute("SELECT mission_id,tool_name FROM mission_tool_approval_decisions").fetchone() == ("m1", "external-action")
    finally:
        conn.close()

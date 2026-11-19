"""V29: persistent terminal-event latch for execution leases (real SQLite)."""
from __future__ import annotations
from uuid import uuid4
import pytest
from tests.v25.test_mission_execution_leases_v25 import add_event, add_mission, database, store

pytestmark = pytest.mark.asyncio


def lease(conn, trace):
    return conn.execute(
        "SELECT cancel_requested_ms, outcome, activated_ms FROM "
        "mission_tool_execution_leases WHERE trace_id=?", (trace,)
    ).fetchone()


async def reserve(conn):
    trace = str(uuid4())
    assert await store(conn).try_start_tool_execution(
        trace_id=trace, mission_id="m1", tool_name="test-tool")
    return trace


async def activate(conn, trace):
    return await store(conn).try_activate_tool_execution(
        trace_id=trace, mission_id="m1", tool_name="test-tool")


# Function (3)
async def test_function_terminal_event_latches_unactivated_reservation(tmp_path):
    c = database(tmp_path / "m.db")
    try:
        add_mission(c); trace = await reserve(c)
        add_event(c, "CANCELLED")
        assert lease(c, trace)[0] is not None
        assert lease(c, trace)[1:] == ("running", None)
        assert not await activate(c, trace)
    finally: c.close()


async def test_function_nonterminal_does_not_latch(tmp_path):
    c = database(tmp_path / "m.db")
    try:
        add_mission(c); trace = await reserve(c)
        add_event(c, "CRITIQUING")
        assert lease(c, trace)[0] is None
        assert await activate(c, trace)
    finally: c.close()


async def test_function_latch_idempotent_and_trace_never_reclaimed(tmp_path):
    c = database(tmp_path / "m.db")
    try:
        add_mission(c); trace = await reserve(c)
        add_event(c, "FAILED"); first = lease(c, trace)[0]
        add_event(c, "TIMED_OUT")
        assert lease(c, trace)[0] == first
        assert not await store(c).try_start_tool_execution(
            trace_id=trace, mission_id="m1", tool_name="test-tool")
    finally: c.close()


# Integration (2): two independent SQLite connections.
async def test_integration_other_connection_cancel_blocks_activation(tmp_path):
    path = tmp_path / "m.db"; a, b = database(path), database(path)
    try:
        add_mission(a); trace = await reserve(a)
        add_event(b, "CANCELLED")
        assert lease(a, trace)[0] is not None
        assert not await activate(a, trace)
    finally: a.close(); b.close()


async def test_integration_other_connection_latches_already_active(tmp_path):
    path = tmp_path / "m.db"; a, b = database(path), database(path)
    try:
        add_mission(a); trace = await reserve(a)
        assert await activate(a, trace)
        add_event(b, "CANCELLED")
        cancel_ms, outcome, activated_ms = lease(a, trace)
        assert cancel_ms is not None and activated_ms is not None
        assert outcome == "running"  # latch does NOT kill an in-flight tool
    finally: a.close(); b.close()


# Security / regression (4)
async def test_security_invalid_event_latches_fail_closed(tmp_path):
    c = database(tmp_path / "m.db")
    try:
        add_mission(c); trace = await reserve(c)
        c.execute("INSERT INTO mission_events (event_id,mission_id,event_type,source_actor,ts_ms,payload_json) "
                  "VALUES (?, 'm1', 'MissionStateChanged', 'ui', 2, '{invalid')", (str(uuid4()),))
        assert lease(c, trace)[0] is not None
        assert not await activate(c, trace)
    finally: c.close()


async def test_security_terminal_named_event_latches(tmp_path):
    c = database(tmp_path / "m.db")
    try:
        add_mission(c); trace = await reserve(c)
        c.execute("INSERT INTO mission_events (event_id,mission_id,event_type,source_actor,ts_ms,payload_json) "
                  "VALUES (?, 'm1', 'MissionCancelled', 'ui', 2, '{}')", (str(uuid4()),))
        assert lease(c, trace)[0] is not None
    finally: c.close()


async def test_security_unrelated_mission_not_latched(tmp_path):
    c = database(tmp_path / "m.db")
    try:
        add_mission(c); trace = await reserve(c)
        c.execute("INSERT INTO mission_events (event_id,mission_id,event_type,source_actor,ts_ms,payload_json) "
                  "VALUES (?, 'other', 'MissionCancelled', 'ui', 2, '{}')", (str(uuid4()),))
        assert lease(c, trace)[0] is None
    finally: c.close()


async def test_security_old_database_migration_preserves_claim(tmp_path):
    c = database(tmp_path / "m.db")
    try:
        c.execute("DROP TABLE mission_tool_execution_leases")
        c.execute("CREATE TABLE mission_tool_execution_leases ("
                  "trace_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL, "
                  "tool_name TEXT NOT NULL, started_ms INTEGER NOT NULL, "
                  "activated_ms INTEGER, finished_ms INTEGER, "
                  "outcome TEXT NOT NULL DEFAULT 'running')")
        c.execute("INSERT INTO mission_tool_execution_leases VALUES "
                  "('old', 'm1', 'test-tool', 1, NULL, NULL, 'running')")
        s = store(c); await s._apply_migrations(); await s._apply_migrations()
        assert 'cancel_requested_ms' in {row[1] for row in c.execute(
            'PRAGMA table_info(mission_tool_execution_leases)')}
        add_mission(c); add_event(c, "CANCELLED")
        assert lease(c, 'old')[0] is not None
        assert not await s.try_start_tool_execution(
            trace_id='old', mission_id='m1', tool_name='test-tool')
    finally: c.close()


async def test_security_pause_then_resume_cannot_reactivate_old_trace(tmp_path):
    c = database(tmp_path / "m.db")
    try:
        add_mission(c); trace = await reserve(c)
        add_event(c, "WAITING_CAPACITY")
        assert lease(c, trace)[0] is not None
        add_event(c, "RUNNING")
        assert not await activate(c, trace)
    finally: c.close()


async def test_integration_rollback_event_rolls_back_latch(tmp_path):
    path = tmp_path / "m.db"; a, b = database(path), database(path)
    try:
        add_mission(a); trace = await reserve(a)
        a.execute("BEGIN IMMEDIATE")
        add_event(a, "CANCELLED")
        assert lease(a, trace)[0] is not None
        assert lease(b, trace)[0] is None  # not visible before commit
        a.execute("ROLLBACK")
        assert lease(b, trace)[0] is None
        assert await activate(b, trace)
    finally: a.close(); b.close()

"""V45: INSERT OR REPLACE cannot reset a previously claimed execution trace.

Real SQLite is used throughout; a test-only async adapter exercises production
MissionEventStore SQL. No external tools or irreversible side effects are run.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest

from tests.v25.test_mission_execution_leases_v25 import (
    FakeTool, add_mission, config, database, setup, store,
)
from jarvis.missions.event_store import _SCHEMA_PATH

pytestmark = pytest.mark.asyncio


def lease(conn, trace):
    return conn.execute(
        "SELECT trace_id, mission_id, tool_name, started_ms, activated_ms, "
        "cancel_requested_ms, finished_ms, outcome "
        "FROM mission_tool_execution_leases WHERE trace_id=?", (trace,)
    ).fetchone()


async def claim(conn, *, tool="external-action"):
    trace = str(uuid4())
    assert await store(conn).try_start_tool_execution(
        trace_id=trace, mission_id="m1", tool_name=tool)
    return trace


def replace(conn, trace, *, tool="different-action", activated=99):
    conn.execute(
        "INSERT OR REPLACE INTO mission_tool_execution_leases "
        "(trace_id, mission_id, tool_name, started_ms, activated_ms) "
        "VALUES (?, 'm1', ?, 999, ?)", (trace, tool, activated)
    )


# FUNCTION (3)
async def test_function_replace_cannot_reset_cancelled_running_trace(tmp_path):
    c = database(tmp_path / "db")
    try:
        add_mission(c); trace = await claim(c)
        c.execute("UPDATE mission_tool_execution_leases SET cancel_requested_ms=123 "
                  "WHERE trace_id=?", (trace,))
        before = lease(c, trace)
        replace(c, trace)
        assert lease(c, trace) == before
    finally:
        c.close()


async def test_function_replace_cannot_reopen_completed_trace(tmp_path):
    c = database(tmp_path / "db")
    try:
        add_mission(c); trace = await claim(c)
        assert await store(c).finish_tool_execution(trace_id=trace, outcome="completed") == "completed"
        before = lease(c, trace)
        replace(c, trace)
        assert lease(c, trace) == before
    finally:
        c.close()


async def test_function_normal_duplicate_claim_is_still_noop_and_new_claim_works(tmp_path):
    c = database(tmp_path / "db")
    try:
        add_mission(c); trace = await claim(c)
        assert not await store(c).try_start_tool_execution(
            trace_id=trace, mission_id="m1", tool_name="external-action")
        assert await claim(c) != trace
        assert c.execute("SELECT count(*) FROM mission_tool_execution_leases").fetchone()[0] == 2
    finally:
        c.close()


# INTEGRATION (3)
async def test_integration_two_connections_replace_cannot_clear_cancel(tmp_path):
    path = tmp_path / "db"; a, b = database(path), database(path)
    try:
        add_mission(a); trace = await claim(a)
        a.execute("UPDATE mission_tool_execution_leases SET cancel_requested_ms=123 "
                  "WHERE trace_id=?", (trace,))
        before = lease(a, trace)
        replace(b, trace)
        assert lease(a, trace) == lease(b, trace) == before
    finally:
        a.close(); b.close()


async def test_integration_separate_os_process_replace_is_noop(tmp_path):
    path = tmp_path / "db"; c = database(path)
    try:
        add_mission(c); trace = await claim(c)
        before = lease(c, trace)
        code = (
            "import sqlite3,sys; "
            "c=sqlite3.connect(sys.argv[1],isolation_level=None); "
            "c.execute('INSERT OR REPLACE INTO mission_tool_execution_leases '"
            "'(trace_id,mission_id,tool_name,started_ms) '"
            "'VALUES (?,\"m1\",\"bad\",999)',(sys.argv[2],)); "
            "c.close()"
        )
        p = subprocess.run([sys.executable, "-c", code, str(path), trace],
                           capture_output=True, text=True, timeout=8)
        assert p.returncode == 0, p.stderr
        assert lease(c, trace) == before
    finally:
        c.close()


async def test_integration_replace_inside_transaction_is_noop(tmp_path):
    path = tmp_path / "db"; a, b = database(path), database(path)
    try:
        add_mission(a); trace = await claim(a); before = lease(a, trace)
        b.execute("BEGIN IMMEDIATE")
        replace(b, trace)
        assert lease(b, trace) == before
        assert lease(a, trace) == before
        b.commit()
        assert lease(a, trace) == before
    finally:
        if b.in_transaction: b.rollback()
        a.close(); b.close()


# SECURITY / REGRESSION (4)
async def test_security_replace_cannot_reenable_cancelled_dispatch(tmp_path):
    c = database(tmp_path / "db")
    try:
        add_mission(c); trace = await claim(c)
        assert await store(c).try_activate_tool_execution(
            trace_id=trace, mission_id="m1", tool_name="external-action")
        c.execute("UPDATE mission_tool_execution_leases SET cancel_requested_ms=123 "
                  "WHERE trace_id=?", (trace,))
        replace(c, trace, tool="external-action", activated=99)
        assert not await store(c).try_fence_tool_dispatch(
            trace_id=trace, mission_id="m1", tool_name="external-action")
        assert lease(c, trace)[5] == 123
    finally:
        c.close()


async def test_security_multirow_replace_ignores_duplicate_but_inserts_fresh(tmp_path):
    c = database(tmp_path / "db")
    try:
        add_mission(c); trace = await claim(c); before = lease(c, trace)
        fresh = str(uuid4())
        c.execute(
            "INSERT OR REPLACE INTO mission_tool_execution_leases "
            "(trace_id,mission_id,tool_name,started_ms) "
            "VALUES (?, 'm1', 'wrong', 999), (?, 'm1', 'new', 12)",
            (trace, fresh),
        )
        assert lease(c, trace) == before
        assert lease(c, fresh)[2:4] == ("new", 12)
    finally:
        c.close()


async def test_security_schema_reload_installs_guard_on_existing_database(tmp_path):
    c = database(tmp_path / "db")
    try:
        add_mission(c); trace = await claim(c); before = lease(c, trace)
        # Simulates an older DB that already has rows but not the new trigger.
        c.execute("DROP TRIGGER IF EXISTS trg_mission_execution_no_duplicate_replace")
        c.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        replace(c, trace)
        assert lease(c, trace) == before
    finally:
        c.close()


async def test_security_real_executor_remains_one_shot_after_replace(tmp_path, monkeypatch):
    c = database(tmp_path / "db")
    try:
        add_mission(c)
        executor, denied = setup(monkeypatch, store(c))
        tool = FakeTool(); trace = uuid4()
        first = await executor.execute(tool, {}, config_snapshot=config(), trace_id=trace)
        assert first.success and tool.calls == 1
        before = lease(c, str(trace))
        replace(c, str(trace))
        second = await executor.execute(tool, {}, config_snapshot=config(), trace_id=trace)
        assert not second.success and tool.calls == 1
        assert denied[-1].reason == "mission_execution_claim_refused"
        assert lease(c, str(trace)) == before
    finally:
        c.close()

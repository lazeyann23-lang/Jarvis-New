"""V30: persistent header-state/delete cancel latch (real SQLite).

The productive SQL schema and MissionEventStore execution SQL are used with
stdlib SQLite; no fake state machine, external tools, or destructive actions.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest

from tests.v25.test_mission_execution_leases_v25 import add_mission, database, store

pytestmark = pytest.mark.asyncio


def status(conn, trace):
    return conn.execute(
        "SELECT cancel_requested_ms, activated_ms, outcome "
        "FROM mission_tool_execution_leases WHERE trace_id=?", (trace,)
    ).fetchone()


async def reserve(conn, mission_id="m1"):
    trace = str(uuid4())
    assert await store(conn).try_start_tool_execution(
        trace_id=trace, mission_id=mission_id, tool_name="test-tool")
    return trace


async def activate(conn, trace):
    return await store(conn).try_activate_tool_execution(
        trace_id=trace, mission_id="m1", tool_name="test-tool")


# Function (3)
async def test_function_direct_header_cancel_latches_before_activation(tmp_path):
    c = database(tmp_path / "missions.db")
    try:
        add_mission(c)
        trace = await reserve(c)
        c.execute("UPDATE missions SET state='CANCELLED' WHERE id='m1'")
        assert status(c, trace)[0] is not None
        assert not await activate(c, trace)
    finally:
        c.close()


async def test_function_active_header_transition_does_not_latch(tmp_path):
    c = database(tmp_path / "missions.db")
    try:
        add_mission(c)
        trace = await reserve(c)
        c.execute("UPDATE missions SET state='CRITIQUING' WHERE id='m1'")
        assert status(c, trace)[0] is None
        assert await activate(c, trace)
    finally:
        c.close()


async def test_function_pause_resume_cannot_reactivate_old_trace(tmp_path):
    c = database(tmp_path / "missions.db")
    try:
        add_mission(c)
        trace = await reserve(c)
        c.execute("UPDATE missions SET state='WAITING_CAPACITY' WHERE id='m1'")
        first = status(c, trace)[0]
        assert first is not None
        c.execute("UPDATE missions SET state='RUNNING' WHERE id='m1'")
        assert status(c, trace)[0] == first
        assert not await activate(c, trace)
    finally:
        c.close()


# Integration (3)
async def test_integration_second_connection_header_change_blocks_first(tmp_path):
    path = tmp_path / "missions.db"
    a, b = database(path), database(path)
    try:
        add_mission(a)
        trace = await reserve(a)
        b.execute("UPDATE missions SET state='FAILED' WHERE id='m1'")
        assert status(a, trace)[0] is not None
        assert not await activate(a, trace)
    finally:
        a.close()
        b.close()


async def test_integration_header_change_rollback_also_rolls_back_latch(tmp_path):
    path = tmp_path / "missions.db"
    a, b = database(path), database(path)
    try:
        add_mission(a)
        trace = await reserve(a)
        b.execute("BEGIN IMMEDIATE")
        b.execute("UPDATE missions SET state='CANCELLED' WHERE id='m1'")
        assert status(b, trace)[0] is not None
        assert status(a, trace)[0] is None
        b.execute("ROLLBACK")
        assert status(a, trace)[0] is None
        assert await activate(a, trace)
    finally:
        a.close()
        b.close()


async def test_integration_independent_os_process_updates_header(tmp_path):
    path = tmp_path / "missions.db"
    c = database(path)
    try:
        add_mission(c)
        trace = await reserve(c)
        code = (
            "import sqlite3,sys; "
            "c=sqlite3.connect(sys.argv[1], isolation_level=None); "
            "c.execute(\"UPDATE missions SET state='TIMED_OUT' WHERE id='m1'\"); "
            "c.close()"
        )
        subprocess.run([sys.executable, "-c", code, str(path)],
                       check=True, capture_output=True, timeout=8)
        assert status(c, trace)[0] is not None
        assert not await activate(c, trace)
    finally:
        c.close()


# Security / regression (4)
async def test_security_delete_then_recreate_does_not_rearm_trace(tmp_path):
    c = database(tmp_path / "missions.db")
    try:
        add_mission(c)
        trace = await reserve(c)
        c.execute("DELETE FROM missions WHERE id='m1'")
        assert status(c, trace)[0] is not None
        add_mission(c)
        assert not await activate(c, trace)
        assert not await store(c).try_start_tool_execution(
            trace_id=trace, mission_id="m1", tool_name="test-tool")
    finally:
        c.close()


async def test_security_unrelated_mission_is_not_latched(tmp_path):
    c = database(tmp_path / "missions.db")
    try:
        add_mission(c)
        trace = await reserve(c)
        c.execute("INSERT INTO missions (id,prompt,state,language,created_ms,updated_ms) "
                  "VALUES ('other','task','RUNNING','de',1,1)")
        c.execute("UPDATE missions SET state='CANCELLED' WHERE id='other'")
        c.execute("DELETE FROM missions WHERE id='other'")
        assert status(c, trace)[0] is None
        assert await activate(c, trace)
    finally:
        c.close()


async def test_security_unknown_state_fails_closed_and_latch_is_idempotent(tmp_path):
    c = database(tmp_path / "missions.db")
    try:
        add_mission(c)
        trace = await reserve(c)
        c.execute("UPDATE missions SET state='CORRUPT' WHERE id='m1'")
        first = status(c, trace)[0]
        assert first is not None
        c.execute("UPDATE missions SET state='FAILED' WHERE id='m1'")
        assert status(c, trace)[0] == first
        assert not await activate(c, trace)
    finally:
        c.close()


async def test_security_finished_execution_not_rewritten_by_header_latch(tmp_path):
    c = database(tmp_path / "missions.db")
    try:
        add_mission(c)
        trace = await reserve(c)
        assert await activate(c, trace)
        await store(c).finish_tool_execution(trace_id=trace, outcome="completed")
        c.execute("UPDATE missions SET state='CANCELLED' WHERE id='m1'")
        assert status(c, trace)[0] is None
        assert status(c, trace)[2] == 'completed'
    finally:
        c.close()


async def test_security_schema_upgrade_adds_missing_header_triggers(tmp_path):
    """Schema load is idempotent on an existing V29 DB (no claim reset)."""
    from jarvis.missions.event_store import _SCHEMA_PATH

    c = database(tmp_path / "missions.db")
    try:
        add_mission(c)
        trace = await reserve(c)
        for name in (
            "trg_mission_execution_header_state_latch",
            "trg_mission_execution_header_delete_latch",
            "trg_mission_execution_header_insert_latch",
        ):
            c.execute(f"DROP TRIGGER {name}")
        c.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        c.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        c.execute("UPDATE missions SET state='CANCELLED' WHERE id='m1'")
        assert status(c, trace)[0] is not None
        assert not await activate(c, trace)
    finally:
        c.close()


async def test_function_productive_upsert_header_change_is_latched(tmp_path):
    c = database(tmp_path / "missions.db")
    try:
        add_mission(c)
        trace = await reserve(c)
        await store(c).upsert_mission(
            mission_id="m1", prompt="task", state="CANCELLED",
            language="de", ts_ms=1234)
        assert status(c, trace)[0] is not None
        assert not await activate(c, trace)
    finally:
        c.close()

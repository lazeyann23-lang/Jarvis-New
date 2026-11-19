"""V26: durable post-claim activation fence; no external tools are invoked.

SQLite tests use two real connections and production SQL through the same
stdlib async adapter as V25; no production aiosqlite driver is installed.
"""
from __future__ import annotations

import asyncio
import sqlite3
from uuid import uuid4

import pytest

from tests.v25.test_mission_execution_leases_v25 import (
    FakeTool, add_event, add_mission, config, database, setup, store,
)
from jarvis.core import runtime_refs

pytestmark = pytest.mark.asyncio


def wire_activation(monkeypatch, s):
    async def activate(trace, mission, tool):
        return await s.try_activate_tool_execution(
            trace_id=str(trace), mission_id=mission, tool_name=tool,
        )
    monkeypatch.setattr(runtime_refs, "get_mission_tool_execution_activator", lambda: activate)
    return activate


# Function (3)
async def test_function_activation_is_single_use_and_audited(tmp_path):
    conn = database(tmp_path / "m.db")
    add_mission(conn)
    s = store(conn)
    trace = str(uuid4())
    assert await s.try_start_tool_execution(trace_id=trace, mission_id="m1", tool_name="x")
    assert await s.try_activate_tool_execution(trace_id=trace, mission_id="m1", tool_name="x")
    assert not await s.try_activate_tool_execution(trace_id=trace, mission_id="m1", tool_name="x")
    assert conn.execute("SELECT activated_ms FROM mission_tool_execution_leases WHERE trace_id=?",
                        (trace,)).fetchone()[0] is not None
    await s.finish_tool_execution(trace_id=trace, outcome="completed")
    assert not await s.try_activate_tool_execution(trace_id=trace, mission_id="m1", tool_name="x")
    conn.close()


async def test_function_executor_uses_activation_and_finishes(tmp_path, monkeypatch):
    conn = database(tmp_path / "m.db")
    add_mission(conn)
    s = store(conn)
    executor, denied = setup(monkeypatch, s)
    wire_activation(monkeypatch, s)
    tool = FakeTool()
    trace = uuid4()
    result = await executor.execute(tool, {}, config_snapshot=config(), trace_id=trace)
    assert result.success and tool.calls == 1 and not denied
    assert conn.execute("SELECT activated_ms,outcome FROM mission_tool_execution_leases WHERE trace_id=?",
                        (str(trace),)).fetchone()[1] == "completed"
    conn.close()


async def test_function_migrates_existing_v25_ledger_without_replay(tmp_path):
    conn = database(tmp_path / "m.db")
    add_mission(conn)
    conn.execute("DROP TABLE mission_tool_execution_leases")
    conn.execute("CREATE TABLE mission_tool_execution_leases ("
                 "trace_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL, "
                 "tool_name TEXT NOT NULL, started_ms INTEGER NOT NULL, "
                 "finished_ms INTEGER, outcome TEXT NOT NULL DEFAULT 'running')")
    conn.execute("INSERT INTO mission_tool_execution_leases VALUES "
                 "('old','m1','x',1,NULL,'running')")
    s = store(conn)
    await s._apply_migrations()
    await s._apply_migrations()  # idempotent
    assert "activated_ms" in {r[1] for r in conn.execute(
        "PRAGMA table_info(mission_tool_execution_leases)")}
    assert not await s.try_start_tool_execution(trace_id="old", mission_id="m1", tool_name="x")
    assert conn.execute("SELECT activated_ms FROM mission_tool_execution_leases WHERE trace_id='old'").fetchone() == (None,)
    conn.close()


# Integration (2): separate real SQLite connections
async def test_integration_cancel_between_claim_and_activation_is_refused(tmp_path):
    path = tmp_path / "m.db"
    a, b = database(path), database(path)
    add_mission(a)
    sa = store(a)
    trace = str(uuid4())
    assert await sa.try_start_tool_execution(trace_id=trace, mission_id="m1", tool_name="x")
    add_event(b, "CANCELLED")  # separate connection, header still RUNNING
    assert not await sa.try_activate_tool_execution(trace_id=trace, mission_id="m1", tool_name="x")
    await sa.finish_tool_execution(trace_id=trace, outcome="cancelled")
    assert a.execute("SELECT activated_ms,outcome FROM mission_tool_execution_leases WHERE trace_id=?",
                     (trace,)).fetchone() == (None, "cancelled")
    assert not await sa.try_start_tool_execution(trace_id=trace, mission_id="m1", tool_name="x")
    a.close(); b.close()


async def test_integration_activation_serialized_between_connections(tmp_path):
    path = tmp_path / "m.db"
    a, b = database(path), database(path)
    add_mission(a)
    sa, sb = store(a), store(b)
    trace = str(uuid4())
    assert await sa.try_start_tool_execution(trace_id=trace, mission_id="m1", tool_name="x")
    results = await asyncio.gather(
        sa.try_activate_tool_execution(trace_id=trace, mission_id="m1", tool_name="x"),
        sb.try_activate_tool_execution(trace_id=trace, mission_id="m1", tool_name="x"),
    )
    assert sorted(results) == [False, True]
    a.close(); b.close()


# Security / regression (4)
async def test_security_wrong_mission_or_tool_cannot_activate(tmp_path):
    conn = database(tmp_path / "m.db")
    add_mission(conn)
    s = store(conn)
    trace = str(uuid4())
    assert await s.try_start_tool_execution(trace_id=trace, mission_id="m1", tool_name="x")
    assert not await s.try_activate_tool_execution(trace_id=trace, mission_id="other", tool_name="x")
    assert not await s.try_activate_tool_execution(trace_id=trace, mission_id="m1", tool_name="other")
    assert await s.try_activate_tool_execution(trace_id=trace, mission_id="m1", tool_name="x")
    conn.close()


async def test_security_corrupt_event_after_claim_blocks_activation(tmp_path):
    conn = database(tmp_path / "m.db")
    add_mission(conn)
    s = store(conn)
    trace = str(uuid4())
    assert await s.try_start_tool_execution(trace_id=trace, mission_id="m1", tool_name="x")
    conn.execute("INSERT INTO mission_events (event_id,mission_id,event_type,source_actor,ts_ms,payload_json) "
                 "VALUES (?, 'm1', 'MissionStateChanged','ui',2,'{invalid')", (str(uuid4()),))
    assert not await s.try_activate_tool_execution(trace_id=trace, mission_id="m1", tool_name="x")
    conn.close()


async def test_security_activation_db_error_denies_without_start(tmp_path, monkeypatch):
    conn = database(tmp_path / "m.db")
    add_mission(conn)
    s = store(conn)
    executor, denied = setup(monkeypatch, s)
    async def fail(*_):
        raise sqlite3.OperationalError("DB unavailable")
    monkeypatch.setattr(runtime_refs, "get_mission_tool_execution_activator", lambda: fail)
    tool = FakeTool()
    trace = uuid4()
    result = await executor.execute(tool, {}, config_snapshot=config(), trace_id=trace)
    assert not result.success and tool.calls == 0
    assert result.error == "approval-denied (mission_inactive_after_execution_claim)"
    assert denied[-1].reason == "mission_inactive_after_execution_claim"
    assert conn.execute("SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?",
                        (str(trace),)).fetchone() == ("cancelled",)
    conn.close()


async def test_security_cancel_after_activation_before_coroutine_start(tmp_path, monkeypatch):
    path = tmp_path / "m.db"
    a, b = database(path), database(path)
    add_mission(a)
    s = store(a)
    executor, denied = setup(monkeypatch, s)
    async def activate_then_cancel(trace, mission, tool):
        result = await s.try_activate_tool_execution(
            trace_id=str(trace), mission_id=mission, tool_name=tool)
        add_event(b, "CANCELLED")
        return result
    monkeypatch.setattr(runtime_refs, "get_mission_tool_execution_activator",
                        lambda: activate_then_cancel)
    tool = FakeTool()
    trace = uuid4()
    result = await executor.execute(tool, {}, config_snapshot=config(), trace_id=trace)
    assert not result.success and tool.calls == 0
    assert result.error == "approval-denied (mission_cancelled_during_execution)"
    assert denied[-1].reason == "mission_cancelled_during_execution"
    assert a.execute("SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?",
                     (str(trace),)).fetchone() == ("cancelled",)
    a.close(); b.close()

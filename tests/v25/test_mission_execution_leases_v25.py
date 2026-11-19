"""V25 persistent one-shot execution leases and cooperative cancel checks.

The production SQL runs on real SQLite with two independent connections via
an async stdlib adapter. No external/irreversible tools are invoked.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import types
from pathlib import Path
from uuid import uuid4

import pytest

try:
    import aiosqlite  # noqa: F401
except ImportError:
    shim = types.ModuleType("aiosqlite")
    for name in ("Connection", "Cursor", "Row"):
        setattr(shim, name, type(name, (), {}))
    sys.modules["aiosqlite"] = shim

from jarvis.core import runtime_refs
from jarvis.core.bus import EventBus
from jarvis.core.config import SafetyConfig
from jarvis.core.events import ActionDenied
from jarvis.core.protocols import ToolResult
from jarvis.missions.event_store import MissionEventStore, _SCHEMA_PATH
from jarvis.safety.approval import ApprovalWorkflow
from jarvis.safety.risk_tier import RiskTierEvaluator
from jarvis.safety.tool_executor import ToolExecutor

pytestmark = pytest.mark.asyncio


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


def database(path):
    conn = sqlite3.connect(path, isolation_level=None, timeout=5)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def add_event(conn, state, mission="m1"):
    conn.execute(
        "INSERT INTO mission_events (event_id, mission_id, event_type, "
        "source_actor, ts_ms, payload_json) "
        "VALUES (?, ?, 'MissionStateChanged', 'ui', 1, ?)",
        (str(uuid4()), mission, '{"to_state":"' + state + '"}'),
    )


def add_mission(conn):
    conn.execute("INSERT INTO missions (id,prompt,state,language,created_ms,updated_ms) "
                 "VALUES ('m1','task','RUNNING','de',1,1)")
    add_event(conn, "RUNNING")


def store(conn):
    result = MissionEventStore(Path("/unused"), bus=None)
    result._conn = AsyncSQLite(conn)
    return result


class FakeTool:
    name = "external-action"
    risk_tier = "safe"
    schema = {"type": "object", "properties": {}}

    def __init__(self):
        self.calls = 0
        self.started = asyncio.Event()
        self.block = False
        self.cancelled = False

    async def execute(self, args, ctx):
        self.calls += 1
        self.started.set()
        if self.block:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return ToolResult(success=True, output={"performed": True})


def setup(monkeypatch, s=None, *, checker=None, finisher=None):
    if s is not None:
        async def claim(tid, mission, tool):
            return await s.try_start_tool_execution(
                trace_id=str(tid), mission_id=mission, tool_name=tool)
        async def finish(tid, outcome):
            return await s.finish_tool_execution(trace_id=str(tid), outcome=outcome)
        checker = s.is_active_for_tool_approval
        finisher = finish
    else:
        claim = None
    monkeypatch.setattr(runtime_refs, "get_mission_tool_execution_checker", lambda: checker)
    monkeypatch.setattr(runtime_refs, "get_mission_tool_execution_claimer", lambda: claim)
    monkeypatch.setattr(runtime_refs, "get_mission_tool_execution_finisher", lambda: finisher)
    bus = EventBus()
    executor = ToolExecutor(bus, RiskTierEvaluator(SafetyConfig(approval_mode="ask")),
                            ApprovalWorkflow(bus))
    denied = []
    async def collect(event):
        denied.append(event)
    bus.subscribe(ActionDenied, collect)
    return executor, denied


def config(mission="m1"):
    return {"tool_origin": "mission_worker", "mission_id": mission}


# Function (3)
async def test_function_first_execution_claim_and_finish(tmp_path, monkeypatch):
    conn = database(tmp_path / "missions.db")
    add_mission(conn)
    executor, denied = setup(monkeypatch, store(conn))
    tool = FakeTool()
    trace = uuid4()
    result = await executor.execute(tool, {}, config_snapshot=config(), trace_id=trace)
    assert result.success and tool.calls == 1 and not denied
    assert conn.execute("SELECT outcome,finished_ms FROM mission_tool_execution_leases "
                        "WHERE trace_id=?", (str(trace),)).fetchone()[0] == "completed"
    conn.close()


async def test_function_chat_does_not_use_execution_lease(monkeypatch):
    executor, denied = setup(monkeypatch, checker=None)
    tool = FakeTool()
    result = await executor.execute(tool, {}, config_snapshot={"tool_origin": "chat"})
    assert result.success and tool.calls == 1 and not denied


async def test_function_replay_denied_even_after_completion(tmp_path, monkeypatch):
    conn = database(tmp_path / "missions.db")
    add_mission(conn)
    executor, denied = setup(monkeypatch, store(conn))
    tool = FakeTool()
    trace = uuid4()
    assert (await executor.execute(tool, {}, config_snapshot=config(), trace_id=trace)).success
    second = await executor.execute(tool, {}, config_snapshot=config(), trace_id=trace)
    assert not second.success and tool.calls == 1
    assert second.error == "approval-denied (mission_execution_claim_refused)"
    assert denied[-1].reason == "mission_execution_claim_refused"
    conn.close()


# Integration (2): two real independent SQLite connections
async def test_integration_second_connection_cannot_reclaim_trace(tmp_path):
    path = tmp_path / "missions.db"
    a, b = database(path), database(path)
    add_mission(a)
    sa, sb = store(a), store(b)
    trace = str(uuid4())
    assert await sa.try_start_tool_execution(trace_id=trace, mission_id="m1", tool_name="x")
    assert not await sb.try_start_tool_execution(trace_id=trace, mission_id="m1", tool_name="x")
    await sa.finish_tool_execution(trace_id=trace, outcome="completed")
    assert not await sb.try_start_tool_execution(trace_id=trace, mission_id="m1", tool_name="x")
    a.close(); b.close()


async def test_integration_other_connection_cancel_before_claim(tmp_path, monkeypatch):
    path = tmp_path / "missions.db"
    a, b = database(path), database(path)
    add_mission(a)
    add_event(b, "CANCELLED")  # stale RUNNING header in the other process
    executor, denied = setup(monkeypatch, store(a))
    tool = FakeTool()
    result = await executor.execute(tool, {}, config_snapshot=config())
    assert not result.success and tool.calls == 0 and denied
    assert a.execute("SELECT count(*) FROM mission_tool_execution_leases").fetchone()[0] == 0
    a.close(); b.close()


# Security / regression (4)
async def test_security_cooperative_cancel_of_running_tool(tmp_path, monkeypatch):
    path = tmp_path / "missions.db"
    a, b = database(path), database(path)
    add_mission(a)
    executor, _ = setup(monkeypatch, store(a))
    tool = FakeTool()
    tool.block = True
    trace = uuid4()
    running = asyncio.create_task(executor.execute(tool, {}, config_snapshot=config(), trace_id=trace))
    await asyncio.wait_for(tool.started.wait(), timeout=2)
    add_event(b, "CANCELLED")
    result = await asyncio.wait_for(running, timeout=3)
    assert not result.success and result.error == "approval-denied (mission_cancelled_during_execution)"
    assert tool.cancelled
    assert a.execute("SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?",
                     (str(trace),)).fetchone() == ("cancelled",)
    a.close(); b.close()


async def test_security_missing_or_corrupt_mission_cannot_claim(tmp_path):
    conn = database(tmp_path / "missions.db")
    s = store(conn)
    assert not await s.try_start_tool_execution(trace_id=str(uuid4()), mission_id="missing", tool_name="x")
    add_mission(conn)
    add_event(conn, "GARBAGE")
    assert not await s.try_start_tool_execution(trace_id=str(uuid4()), mission_id="m1", tool_name="x")
    assert conn.execute("SELECT count(*) FROM mission_tool_execution_leases").fetchone()[0] == 0
    conn.close()


async def test_security_db_failure_denies_without_tool_execution(monkeypatch):
    async def active(_): return True
    async def broken(*_): raise sqlite3.OperationalError("database locked")
    async def finish(*_): pass
    executor, denied = setup(monkeypatch, checker=active)
    monkeypatch.setattr(runtime_refs, "get_mission_tool_execution_claimer", lambda: broken)
    monkeypatch.setattr(runtime_refs, "get_mission_tool_execution_finisher", lambda: finish)
    tool = FakeTool()
    result = await executor.execute(tool, {}, config_snapshot=config())
    assert not result.success and tool.calls == 0
    assert denied[-1].reason == "mission_execution_claim_refused"


async def test_security_cancelled_task_keeps_non_replayable_claim(tmp_path, monkeypatch):
    conn = database(tmp_path / "missions.db")
    add_mission(conn)
    executor, _ = setup(monkeypatch, store(conn))
    tool = FakeTool()
    tool.block = True
    trace = uuid4()
    running = asyncio.create_task(executor.execute(tool, {}, config_snapshot=config(), trace_id=trace))
    await asyncio.wait_for(tool.started.wait(), timeout=2)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert tool.cancelled
    assert conn.execute("SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?",
                        (str(trace),)).fetchone() == ("cancelled",)
    assert not await store(conn).try_start_tool_execution(
        trace_id=str(trace), mission_id="m1", tool_name="external-action")
    conn.close()

async def test_security_local_cancel_token_stops_running_mission_tool(tmp_path, monkeypatch):
    from jarvis.control.cancel import CancelToken as ConcreteCancelToken

    conn = database(tmp_path / "missions.db")
    add_mission(conn)
    executor, denied = setup(monkeypatch, store(conn))
    tool = FakeTool()
    tool.block = True
    token = ConcreteCancelToken()
    trace = uuid4()
    running = asyncio.create_task(executor.execute(
        tool, {}, config_snapshot=config(), trace_id=trace, cancel_token=token))
    await asyncio.wait_for(tool.started.wait(), timeout=2)
    token.cancel("user_stop")
    result = await asyncio.wait_for(running, timeout=3)
    assert not result.success and tool.cancelled
    assert denied[-1].reason == "mission_cancelled_during_execution"
    assert conn.execute("SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?",
                        (str(trace),)).fetchone() == ("cancelled",)
    conn.close()

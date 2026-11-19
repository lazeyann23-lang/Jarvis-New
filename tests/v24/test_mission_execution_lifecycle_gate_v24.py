"""V24 final execution gate tests; SQLite via stdlib adapter, no external actions."""
import sqlite3
import sys
import types
from pathlib import Path
from uuid import uuid4

import pytest

try:
    import aiosqlite
except ImportError:
    shim = types.ModuleType("aiosqlite")
    for name in ("Connection", "Cursor", "Row"):
        setattr(shim, name, type(name, (), {}))
    sys.modules["aiosqlite"] = shim

from jarvis.core import runtime_refs
from jarvis.core.bus import EventBus
from jarvis.core.config import SafetyConfig
from jarvis.core.events import ActionApprovalRequired, ActionApproved, ActionDenied
from jarvis.core.protocols import ToolResult
from jarvis.missions.event_store import MissionEventStore, _SCHEMA_PATH
from jarvis.safety.approval import ApprovalWorkflow
from jarvis.safety.risk_tier import RiskTierEvaluator
from jarvis.safety.tool_executor import ToolExecutor

pytestmark = pytest.mark.asyncio


class FakeTool:
    name = "external-action"
    risk_tier = "safe"
    schema = {"type": "object", "properties": {}}

    def __init__(self, tier="safe"):
        self.risk_tier = tier
        self.calls = 0

    async def execute(self, args, ctx):
        self.calls += 1
        return ToolResult(success=True, output={"performed": True})


def setup(monkeypatch, checker):
    monkeypatch.setattr(runtime_refs, "get_mission_tool_execution_checker", lambda: checker)
    bus = EventBus()
    executor = ToolExecutor(bus, RiskTierEvaluator(SafetyConfig(approval_mode="ask")), ApprovalWorkflow(bus))
    denials = []

    async def collect(event):
        denials.append(event)

    bus.subscribe(ActionDenied, collect)
    return executor, bus, denials


def config(mission="m1"):
    return {"tool_origin": "mission_worker", "mission_id": mission}


class AsyncCursor:
    def __init__(self, cursor): self.cursor = cursor
    async def fetchall(self): return self.cursor.fetchall()
    async def close(self): self.cursor.close()


class AsyncSQLite:
    def __init__(self, conn): self.conn = conn
    async def execute(self, sql, params=()): return AsyncCursor(self.conn.execute(sql, params))


def database(path):
    conn = sqlite3.connect(path, isolation_level=None, timeout=5)
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def event(conn, state):
    conn.execute("INSERT INTO mission_events (event_id,mission_id,event_type,source_actor,ts_ms,payload_json) VALUES (?, 'm1','MissionStateChanged','ui',1,?)", (str(uuid4()), '{"to_state":"' + state + '"}'))


def add_mission(conn):
    conn.execute("INSERT INTO missions (id,prompt,state,language,created_ms,updated_ms) VALUES ('m1','task','RUNNING','de',1,1)")
    event(conn, "RUNNING")


def checker(conn):
    store = MissionEventStore(Path("/unused"), bus=None)
    store._conn = AsyncSQLite(conn)
    return store.is_active_for_tool_approval


# Function tests
async def test_function_active_executes(monkeypatch):
    calls = []
    async def active(mid): calls.append(mid); return True
    executor, _, denials = setup(monkeypatch, active)
    tool = FakeTool()
    result = await executor.execute(tool, {}, config_snapshot=config())
    assert result.success and tool.calls == 1 and calls == ["m1"] and not denials


async def test_function_chat_unaffected(monkeypatch):
    executor, _, denials = setup(monkeypatch, None)
    tool = FakeTool()
    result = await executor.execute(tool, {}, config_snapshot={"tool_origin": "chat"})
    assert result.success and tool.calls == 1 and not denials


async def test_function_inactive_refused(monkeypatch):
    async def inactive(_): return False
    executor, _, denials = setup(monkeypatch, inactive)
    tool = FakeTool()
    result = await executor.execute(tool, {}, config_snapshot=config())
    assert not result.success and tool.calls == 0
    assert result.error == "approval-denied (mission_inactive_before_execution)"
    assert [e.reason for e in denials] == ["mission_inactive_before_execution"]


# Integration: actual persisted event log, two SQLite connections
async def test_integration_remote_cancel_while_approval_pending(monkeypatch, tmp_path):
    path = tmp_path / "mission.db"
    first, second = database(path), database(path)
    try:
        add_mission(first)
        executor, bus, denials = setup(monkeypatch, checker(first))
        tool = FakeTool("ask")
        async def approve_after_cancel(e: ActionApprovalRequired):
            event(second, "CANCELLED")
            await bus.publish(ActionApproved(trace_id=e.trace_id, tool_name=e.tool_name, approved_by="human"))
        bus.subscribe(ActionApprovalRequired, approve_after_cancel)
        result = await executor.execute(tool, {}, config_snapshot={**config(), "approval_surface": "interactive"})
        assert not result.success and tool.calls == 0
        assert any(e.reason == "mission_inactive_before_execution" for e in denials)
    finally:
        first.close(); second.close()


async def test_integration_active_sqlite_after_human_approval(monkeypatch, tmp_path):
    conn = database(tmp_path / "mission.db")
    try:
        add_mission(conn)
        executor, bus, denials = setup(monkeypatch, checker(conn))
        tool = FakeTool("ask")
        async def approve(e: ActionApprovalRequired):
            await bus.publish(ActionApproved(trace_id=e.trace_id, tool_name=e.tool_name, approved_by="human"))
        bus.subscribe(ActionApprovalRequired, approve)
        result = await executor.execute(tool, {}, config_snapshot={**config(), "approval_surface": "interactive"})
        assert result.success and tool.calls == 1 and not denials
    finally: conn.close()


# Security / regression
@pytest.mark.parametrize("mode", ["missing", "exception", "empty_id"])
async def test_security_missing_broken_or_empty_state_fails_closed(monkeypatch, mode):
    async def broken(_): raise sqlite3.OperationalError("database locked")
    checker_fn = None if mode == "missing" else broken
    executor, _, denials = setup(monkeypatch, checker_fn)
    tool = FakeTool()
    result = await executor.execute(tool, {}, config_snapshot=config("" if mode == "empty_id" else "m1"))
    assert not result.success and tool.calls == 0 and len(denials) == 1


async def test_security_stale_running_header_denied(monkeypatch, tmp_path):
    conn = database(tmp_path / "mission.db")
    try:
        add_mission(conn)
        event(conn, "CANCELLED")  # header remains RUNNING
        executor, _, _ = setup(monkeypatch, checker(conn))
        tool = FakeTool()
        result = await executor.execute(tool, {}, config_snapshot=config())
        assert not result.success and tool.calls == 0
    finally: conn.close()

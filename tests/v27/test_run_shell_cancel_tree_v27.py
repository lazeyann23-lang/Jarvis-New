"""V27: run_shell must reap subprocess trees on timeout / mission cancellation.

Real POSIX subprocesses are used; Windows taskkill behaviour is unit-mocked,
not claimed as Windows end-to-end validation. No destructive commands run.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shlex
import sqlite3
import sys
import types
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
from jarvis.missions.event_store import MissionEventStore, _SCHEMA_PATH
from jarvis.plugins.tool import run_shell
from jarvis.plugins.tool.run_shell import RunShellTool
from jarvis.safety.approval import ApprovalWorkflow
from jarvis.safety.risk_tier import RiskTierEvaluator
from jarvis.safety.tool_executor import ToolExecutor

pytestmark = pytest.mark.asyncio
posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")


async def wait_for_file(path: Path) -> None:
    async def check():
        while not path.exists() or path.stat().st_size == 0:
            await asyncio.sleep(0.02)
    await asyncio.wait_for(check(), timeout=3)


def heartbeat_command(tmp_path: Path, *, detached_shell: bool = False) -> tuple[str, Path]:
    marker = tmp_path / "heartbeat.txt"
    script = tmp_path / "child.py"
    script.write_text(
        "import pathlib, time\n"
        f"p = pathlib.Path({str(marker)!r})\n"
        "while True:\n"
        "    with p.open('a') as f: f.write('x')\n"
        "    time.sleep(0.03)\n",
        encoding="utf-8",
    )
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))} &"
    if detached_shell:
        # Parent exits only after the child has started writing. The child
        # still holds stdout/stderr open, so communicate() must time out.
        return command + f" while [ ! -s {shlex.quote(str(marker))} ]; do sleep 0.02; done; exit 0", marker
    return command + " wait", marker


async def assert_stopped(marker: Path) -> None:
    # A living child writes every 30 ms. A cancelled shell must not leave one.
    await asyncio.sleep(0.15)
    before = marker.stat().st_size
    await asyncio.sleep(0.25)
    assert marker.stat().st_size == before, "orphan child is still running"


class AsyncCursor:
    def __init__(self, cursor): self.cursor = cursor
    async def fetchone(self): return self.cursor.fetchone()
    async def fetchall(self): return self.cursor.fetchall()
    async def close(self): self.cursor.close()


class AsyncSQLite:
    def __init__(self, conn): self.conn = conn
    async def execute(self, sql, params=()): return AsyncCursor(self.conn.execute(sql, params))


def database(path: Path):
    conn = sqlite3.connect(path, isolation_level=None, timeout=5)
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def mission(conn):
    conn.execute("INSERT INTO missions (id,prompt,state,language,created_ms,updated_ms) "
                 "VALUES ('m1','task','RUNNING','de',1,1)")
    state_event(conn, "RUNNING")


def state_event(conn, state):
    conn.execute("INSERT INTO mission_events (event_id,mission_id,event_type,"
                 "source_actor,ts_ms,payload_json) "
                 "VALUES (?, 'm1', 'MissionStateChanged', 'ui', 1, ?)",
                 (str(uuid4()), '{"to_state":"' + state + '"}'))


class SafeShell(RunShellTool):
    risk_tier = "safe"


def executor_with_store(monkeypatch, conn):
    store = MissionEventStore(Path("/unused"), bus=None)
    store._conn = AsyncSQLite(conn)
    async def claim(tid, mid, tool):
        return await store.try_start_tool_execution(trace_id=str(tid), mission_id=mid, tool_name=tool)
    async def activate(tid, mid, tool):
        return await store.try_activate_tool_execution(trace_id=str(tid), mission_id=mid, tool_name=tool)
    async def finish(tid, outcome):
        await store.finish_tool_execution(trace_id=str(tid), outcome=outcome)
    monkeypatch.setattr(runtime_refs, "get_mission_tool_execution_checker", lambda: store.is_active_for_tool_approval)
    monkeypatch.setattr(runtime_refs, "get_mission_tool_execution_claimer", lambda: claim)
    monkeypatch.setattr(runtime_refs, "get_mission_tool_execution_activator", lambda: activate)
    monkeypatch.setattr(runtime_refs, "get_mission_tool_execution_finisher", lambda: finish)
    bus = EventBus()
    return ToolExecutor(bus, RiskTierEvaluator(SafetyConfig(approval_mode="ask")), ApprovalWorkflow(bus))


# Function (3)
@posix
async def test_function_normal_command_and_pipe_unchanged():
    result = await RunShellTool().execute({"command": "printf hello | tr a-z A-Z"}, None)
    assert result.success and result.output["stdout"] == "HELLO"


@posix
async def test_function_timeout_returns_failure(tmp_path):
    command, marker = heartbeat_command(tmp_path)
    result = await RunShellTool().execute({"command": command, "timeout_s": 1.5}, None)
    assert not result.success and "Timeout" in result.error
    await wait_for_file(marker)
    await assert_stopped(marker)


async def test_function_windows_cleanup_requests_tree_kill(monkeypatch):
    calls = []
    class FakeProcess:
        pid = 321
        returncode = None
        def kill(self): calls.append("kill")
        async def wait(self): calls.append("wait")
    async def fake_spawn(*args, **kwargs):
        calls.append(args)
        return FakeProcess()
    monkeypatch.setattr(run_shell.sys, "platform", "win32")
    monkeypatch.setattr(run_shell.asyncio, "create_subprocess_exec", fake_spawn)
    await run_shell._stop_shell_process(FakeProcess())
    assert ("taskkill", "/PID", "321", "/T", "/F") in calls
    assert "wait" in calls


# Integration (2): real executor -> real run_shell -> real SQLite two connections
@posix
async def test_integration_mission_cancel_kills_real_shell_child(tmp_path, monkeypatch):
    a = database(tmp_path / "missions.db")
    b = database(tmp_path / "missions.db")
    mission(a)
    executor = executor_with_store(monkeypatch, a)
    command, marker = heartbeat_command(tmp_path)
    trace = uuid4()
    task = asyncio.create_task(executor.execute(
        SafeShell(), {"command": command, "timeout_s": 10},
        config_snapshot={"tool_origin": "mission_worker", "mission_id": "m1"},
        trace_id=trace,
    ))
    try:
        await wait_for_file(marker)
        state_event(b, "CANCELLED")
        result = await asyncio.wait_for(task, timeout=4)
        assert not result.success and "mission_cancelled_during_execution" in result.error
        assert a.execute("SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?",
                         (str(trace),)).fetchone() == ("cancelled",)
        await assert_stopped(marker)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        a.close(); b.close()


@posix
async def test_integration_executor_task_cancel_kills_shell_child(tmp_path, monkeypatch):
    a = database(tmp_path / "missions.db")
    mission(a)
    executor = executor_with_store(monkeypatch, a)
    command, marker = heartbeat_command(tmp_path)
    trace = uuid4()
    task = asyncio.create_task(executor.execute(
        SafeShell(), {"command": command, "timeout_s": 10},
        config_snapshot={"tool_origin": "mission_worker", "mission_id": "m1"},
        trace_id=trace,
    ))
    try:
        await wait_for_file(marker)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=4)
        assert a.execute("SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?",
                         (str(trace),)).fetchone() == ("cancelled",)
        await assert_stopped(marker)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        a.close()


# Security / regression (4)
@posix
async def test_security_cancel_direct_shell_kills_child(tmp_path):
    command, marker = heartbeat_command(tmp_path)
    task = asyncio.create_task(RunShellTool().execute({"command": command, "timeout_s": 10}, None))
    try:
        await wait_for_file(marker)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=4)
        await assert_stopped(marker)
    finally:
        if not task.done(): task.cancel()


@posix
async def test_security_timeout_kills_orphan_after_parent_exit(tmp_path):
    command, marker = heartbeat_command(tmp_path, detached_shell=True)
    result = await RunShellTool().execute({"command": command, "timeout_s": 1.5}, None)
    assert not result.success and "Timeout" in result.error
    await wait_for_file(marker)
    await assert_stopped(marker)


async def test_security_windows_taskkill_missing_falls_back_to_direct_kill(monkeypatch):
    calls = []
    class FakeProcess:
        pid = 444
        returncode = None
        def kill(self): calls.append("kill")
        async def wait(self): calls.append("wait")
    async def missing(*args, **kwargs): raise FileNotFoundError("taskkill")
    monkeypatch.setattr(run_shell.sys, "platform", "win32")
    monkeypatch.setattr(run_shell.asyncio, "create_subprocess_exec", missing)
    await run_shell._stop_shell_process(FakeProcess())
    assert calls == ["kill", "wait"]


@posix
async def test_security_success_does_not_kill_new_shell(tmp_path):
    marker = tmp_path / "normal.txt"
    command = f"printf safe > {shlex.quote(str(marker))}"
    result = await RunShellTool().execute({"command": command}, None)
    assert result.success and marker.read_text() == "safe"

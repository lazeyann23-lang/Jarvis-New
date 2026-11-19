"""V39: a completed cancel watcher wins over a simultaneous tool result.

The test-only wait barrier deterministically exercises the otherwise narrow
same-event-loop-turn race. SQLite tests use production SQL with the existing
stdlib async test adapter; no production aiosqlite or OS tool side effects.
"""
from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest

from jarvis.control.cancel import CancelToken
from jarvis.core import runtime_refs
from jarvis.core.protocols import ExecutionContext, ToolResult
from jarvis.safety import tool_executor as executor_module
from tests.v25.test_mission_execution_leases_v25 import (
    add_event, add_mission, config, database, setup, store,
)
from tests.v31.test_running_execution_cancel_latch_v31 import wired

pytestmark = pytest.mark.asyncio


class ReleasedTool:
    name = 'external-action'
    risk_tier = 'safe'
    schema = {'type': 'object', 'properties': {}}

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.cancelled = False

    async def execute(self, args, ctx):
        self.calls += 1
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return ToolResult(success=True, output={'already_performed': True})


def force_simultaneous_completion(monkeypatch):
    """Make the FIRST_COMPLETED call return only after both tasks are ready.

    This controls scheduling, not production logic; without V39 the executor
    incorrectly accepts the successful tool result in this exact condition.
    """
    original = asyncio.wait

    async def wait(tasks, *, timeout=None, return_when=asyncio.ALL_COMPLETED):
        if return_when == asyncio.FIRST_COMPLETED:
            return await asyncio.wait_for(
                original(tasks, timeout=timeout, return_when=asyncio.ALL_COMPLETED),
                timeout=3,
            )
        return await original(tasks, timeout=timeout, return_when=return_when)

    monkeypatch.setattr(executor_module.asyncio, 'wait', wait)


def ctx():
    return ExecutionContext(trace_id=uuid4(), user_utterance='', config={}, memory_read=None)


async def direct_executor(monkeypatch):
    executor, _ = setup(monkeypatch, checker=None)
    return executor


async def launch(executor, tool, *, trace=None, token=None):
    trace = trace or uuid4()
    task = asyncio.create_task(executor.execute(
        tool, {}, config_snapshot=config(), trace_id=trace, cancel_token=token,
    ))
    await asyncio.wait_for(tool.started.wait(), 2)
    return trace, task


async def assert_cancelled_result(task):
    result = await asyncio.wait_for(task, 4)
    assert not result.success
    assert result.error == 'approval-denied (mission_cancelled_during_execution)'
    return result


# FUNCTION (3): actual watcher and tool coroutines, controlled completion order.
async def test_function_simultaneous_watch_and_success_prefers_cancel(monkeypatch):
    executor = await direct_executor(monkeypatch)
    force_simultaneous_completion(monkeypatch)
    checks = 0

    async def checker(_):
        nonlocal checks
        checks += 1
        return checks == 1  # preflight succeeds, watcher observes cancellation

    tool = ReleasedTool()
    task = asyncio.create_task(executor._execute_with_mission_cancel_watch(
        tool, {}, ctx(), 'm1', checker,
    ))
    await asyncio.wait_for(tool.started.wait(), 2)
    tool.release.set()
    await assert_cancelled_result(task)
    assert tool.calls == 1 and not tool.cancelled and checks >= 2


async def test_function_success_without_watch_completion_still_succeeds(monkeypatch):
    executor = await direct_executor(monkeypatch)

    async def checker(_):
        return True

    tool = ReleasedTool()
    task = asyncio.create_task(executor._execute_with_mission_cancel_watch(
        tool, {}, ctx(), 'm1', checker,
    ))
    await asyncio.wait_for(tool.started.wait(), 2)
    tool.release.set()
    result = await asyncio.wait_for(task, 2)
    assert result.success and tool.calls == 1 and not tool.cancelled


async def test_function_watcher_finishes_first_cancels_blocked_tool(monkeypatch):
    executor = await direct_executor(monkeypatch)
    checks = 0

    async def checker(_):
        nonlocal checks
        checks += 1
        return checks == 1

    tool = ReleasedTool()
    task = asyncio.create_task(executor._execute_with_mission_cancel_watch(
        tool, {}, ctx(), 'm1', checker,
    ))
    await asyncio.wait_for(tool.started.wait(), 2)
    await assert_cancelled_result(task)
    assert tool.cancelled and tool.calls == 1


# INTEGRATION (3): production ToolExecutor + SQLite, two independent writers.
async def test_integration_header_pause_resume_same_turn_denies_success(tmp_path, monkeypatch):
    path = tmp_path / 'missions.db'
    a, b = database(path), database(path)
    try:
        add_mission(a)
        executor, denied = wired(monkeypatch, store(a))
        force_simultaneous_completion(monkeypatch)
        tool = ReleasedTool()
        trace, task = await launch(executor, tool)
        b.execute("UPDATE missions SET state='WAITING_CAPACITY' WHERE id='m1'")
        b.execute("UPDATE missions SET state='RUNNING' WHERE id='m1'")
        tool.release.set()
        await assert_cancelled_result(task)
        assert a.execute('SELECT outcome,cancel_requested_ms FROM mission_tool_execution_leases '
                         'WHERE trace_id=?', (str(trace),)).fetchone()[0] == 'cancelled'
        assert a.execute('SELECT cancel_requested_ms FROM mission_tool_execution_leases '
                         'WHERE trace_id=?', (str(trace),)).fetchone()[0] is not None
        assert denied[-1].reason == 'mission_cancelled_during_execution'
    finally:
        a.close(); b.close()


async def test_integration_separate_process_pause_resume_same_turn(tmp_path, monkeypatch):
    path = tmp_path / 'missions.db'
    a, b = database(path), database(path)
    try:
        add_mission(a)
        executor, _ = wired(monkeypatch, store(a))
        force_simultaneous_completion(monkeypatch)
        tool = ReleasedTool()
        trace, task = await launch(executor, tool)
        code = ("import sqlite3,sys; c=sqlite3.connect(sys.argv[1],isolation_level=None); "
                "c.execute(\"UPDATE missions SET state='WAITING_CAPACITY' WHERE id='m1'\"); "
                "c.execute(\"UPDATE missions SET state='RUNNING' WHERE id='m1'\"); c.close()")
        await asyncio.to_thread(subprocess.run, [sys.executable, '-c', code, str(path)],
                                check=True, capture_output=True, timeout=6)
        tool.release.set()
        await assert_cancelled_result(task)
        assert b.execute('SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?',
                         (str(trace),)).fetchone() == ('cancelled',)
    finally:
        a.close(); b.close()


async def test_integration_eventlog_pause_resume_same_turn(tmp_path, monkeypatch):
    path = tmp_path / 'missions.db'
    a, b = database(path), database(path)
    try:
        add_mission(a)
        executor, _ = wired(monkeypatch, store(a))
        force_simultaneous_completion(monkeypatch)
        tool = ReleasedTool()
        trace, task = await launch(executor, tool)
        add_event(b, 'WAITING_CAPACITY')
        add_event(b, 'RUNNING')
        tool.release.set()
        await assert_cancelled_result(task)
        assert b.execute('SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?',
                         (str(trace),)).fetchone() == ('cancelled',)
    finally:
        a.close(); b.close()


# SECURITY / REGRESSION (4): fail-closed DB, kill switch, replay and isolation.
async def test_security_db_failure_during_watch_does_not_accept_success(tmp_path, monkeypatch):
    c = database(tmp_path / 'missions.db')
    try:
        add_mission(c)
        executor, _ = wired(monkeypatch, store(c))
        force_simultaneous_completion(monkeypatch)
        calls = 0

        async def flaky_lease(tid, mid, tool):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise sqlite3.OperationalError('simulated DB outage')
            return True

        monkeypatch.setattr(runtime_refs, 'get_mission_tool_execution_lease_checker',
                            lambda: flaky_lease)
        tool = ReleasedTool()
        trace, task = await launch(executor, tool)
        tool.release.set()
        await assert_cancelled_result(task)
        assert c.execute('SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?',
                         (str(trace),)).fetchone() == ('cancelled',)
    finally:
        c.close()


async def test_security_cancel_token_same_turn_denies_success(tmp_path, monkeypatch):
    c = database(tmp_path / 'missions.db')
    try:
        add_mission(c)
        executor, _ = wired(monkeypatch, store(c))
        force_simultaneous_completion(monkeypatch)
        token = CancelToken()
        tool = ReleasedTool()
        trace, task = await launch(executor, tool, token=token)
        token.cancel('user')
        tool.release.set()
        await assert_cancelled_result(task)
        assert c.execute('SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?',
                         (str(trace),)).fetchone() == ('cancelled',)
    finally:
        c.close()


async def test_security_simultaneous_cancelled_trace_not_replayed(tmp_path, monkeypatch):
    path = tmp_path / 'missions.db'
    a, b = database(path), database(path)
    try:
        add_mission(a)
        executor, denied = wired(monkeypatch, store(a))
        force_simultaneous_completion(monkeypatch)
        tool = ReleasedTool()
        trace, task = await launch(executor, tool)
        b.execute("UPDATE missions SET state='WAITING_CAPACITY' WHERE id='m1'")
        b.execute("UPDATE missions SET state='RUNNING' WHERE id='m1'")
        tool.release.set()
        await assert_cancelled_result(task)
        second = await executor.execute(tool, {}, config_snapshot=config(), trace_id=trace)
        assert not second.success and tool.calls == 1
        assert denied[-1].reason == 'mission_execution_claim_refused'
    finally:
        a.close(); b.close()


async def test_security_unrelated_mission_pause_does_not_cancel(tmp_path, monkeypatch):
    path = tmp_path / 'missions.db'
    a, b = database(path), database(path)
    try:
        add_mission(a)
        a.execute("INSERT INTO missions (id,prompt,state,language,created_ms,updated_ms) "
                  "VALUES ('other','task','RUNNING','de',1,1)")
        executor, _ = wired(monkeypatch, store(a))
        tool = ReleasedTool()
        trace, task = await launch(executor, tool)
        b.execute("UPDATE missions SET state='CANCELLED' WHERE id='other'")
        tool.release.set()
        result = await asyncio.wait_for(task, 2)
        assert result.success
        assert a.execute('SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?',
                         (str(trace),)).fetchone() == ('completed',)
    finally:
        a.close(); b.close()

"""V40: post-result cancellation fence for a fast mission tool.

The 250 ms watcher can miss a cancellation committed between its last read
and tool completion.  These tests commit cancellation *inside* the tool before
its successful result, so the gap is deterministic without sleep races.
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
from tests.v25.test_mission_execution_leases_v25 import (
    add_event, add_mission, config, database, setup, store,
)
from tests.v31.test_running_execution_cancel_latch_v31 import wired

pytestmark = pytest.mark.asyncio


class FastTool:
    name = 'external-action'
    risk_tier = 'safe'
    schema = {'type': 'object', 'properties': {}}

    def __init__(self, on_execute=None):
        self.calls = 0
        self.on_execute = on_execute

    async def execute(self, args, ctx):
        self.calls += 1
        if self.on_execute is not None:
            action = self.on_execute()
            if asyncio.iscoroutine(action):
                await action
        return ToolResult(success=True, output={'external_side_effect_may_exist': True})


def context():
    return ExecutionContext(trace_id=uuid4(), user_utterance='', config={}, memory_read=None)


async def direct(monkeypatch):
    executor, _ = setup(monkeypatch)
    return executor


async def cancelled(task):
    result = await asyncio.wait_for(task, 3)
    assert not result.success
    assert result.error == 'approval-denied (mission_cancelled_during_execution)'
    return result


# FUNCTION: no SQLite; watcher cannot poll before an immediate tool completion.
async def test_function_postresult_rejects_fast_success_after_cancel(monkeypatch):
    executor = await direct(monkeypatch)
    checks = 0

    async def checker(_):
        nonlocal checks
        checks += 1
        return checks == 1

    tool = FastTool()
    await cancelled(executor._execute_with_mission_cancel_watch(
        tool, {}, context(), 'm1', checker,
    ))
    assert tool.calls == 1 and checks >= 2


async def test_function_postresult_allows_still_active_mission(monkeypatch):
    executor = await direct(monkeypatch)
    checks = 0

    async def checker(_):
        nonlocal checks
        checks += 1
        return True

    tool = FastTool()
    result = await executor._execute_with_mission_cancel_watch(
        tool, {}, context(), 'm1', checker,
    )
    assert result.success and tool.calls == 1 and checks >= 2


async def test_function_postresult_checker_failure_fails_closed(monkeypatch):
    executor = await direct(monkeypatch)
    checks = 0

    async def checker(_):
        nonlocal checks
        checks += 1
        if checks > 1:
            raise sqlite3.OperationalError('read failed')
        return True

    tool = FastTool()
    await cancelled(executor._execute_with_mission_cancel_watch(
        tool, {}, context(), 'm1', checker,
    ))
    assert tool.calls == 1 and checks >= 2


# INTEGRATION: production SQL, real independent SQLite connections and OS writer.
async def test_integration_second_connection_header_pause_resume(tmp_path, monkeypatch):
    path = tmp_path / 'missions.db'
    a, b = database(path), database(path)
    try:
        add_mission(a)
        executor, denied = wired(monkeypatch, store(a))

        def pause_resume():
            b.execute("UPDATE missions SET state='WAITING_CAPACITY' WHERE id='m1'")
            b.execute("UPDATE missions SET state='RUNNING' WHERE id='m1'")

        tool = FastTool(pause_resume)
        trace = uuid4()
        await cancelled(executor.execute(tool, {}, config_snapshot=config(), trace_id=trace))
        assert a.execute('SELECT outcome,cancel_requested_ms FROM mission_tool_execution_leases '
                         'WHERE trace_id=?', (str(trace),)).fetchone()[0] == 'cancelled'
        assert a.execute('SELECT cancel_requested_ms FROM mission_tool_execution_leases '
                         'WHERE trace_id=?', (str(trace),)).fetchone()[0] is not None
        assert denied[-1].reason == 'mission_cancelled_during_execution'
        replay = await executor.execute(tool, {}, config_snapshot=config(), trace_id=trace)
        assert not replay.success and tool.calls == 1
    finally:
        a.close(); b.close()


async def test_integration_external_process_event_pause_resume(tmp_path, monkeypatch):
    path = tmp_path / 'missions.db'
    a, b = database(path), database(path)
    try:
        add_mission(a)
        executor, _ = wired(monkeypatch, store(a))

        def external_event_writer():
            code = """import sqlite3,sys,uuid,json
c=sqlite3.connect(sys.argv[1],isolation_level=None)
for state in ('WAITING_CAPACITY','RUNNING'):
    c.execute('INSERT INTO mission_events (event_id,mission_id,event_type,source_actor,ts_ms,payload_json) VALUES (?, ?, ?, ?, ?, ?)', (str(uuid.uuid4()), 'm1', 'MissionStateChanged', 'ui', 2, json.dumps({'to_state':state})))
c.close()
"""
            # Intentionally block this test event loop while the separate OS
            # writer commits; the 250 ms watcher cannot poll in this gap.
            subprocess.run([sys.executable, '-c', code, str(path)],
                           check=True, capture_output=True, timeout=6)

        tool = FastTool(external_event_writer)
        trace = uuid4()
        await cancelled(executor.execute(tool, {}, config_snapshot=config(), trace_id=trace))
        assert b.execute('SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?',
                         (str(trace),)).fetchone() == ('cancelled',)
        assert tool.calls == 1
    finally:
        a.close(); b.close()


async def test_integration_fast_success_with_valid_lease(tmp_path, monkeypatch):
    path = tmp_path / 'missions.db'
    a, b = database(path), database(path)
    try:
        add_mission(a)
        executor, _ = wired(monkeypatch, store(a))
        tool = FastTool()
        trace = uuid4()
        result = await executor.execute(tool, {}, config_snapshot=config(), trace_id=trace)
        assert result.success and tool.calls == 1
        assert b.execute('SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?',
                         (str(trace),)).fetchone() == ('completed',)
    finally:
        a.close(); b.close()


# SECURITY / REGRESSION: fail-closed, kill-switch, isolation and nonmission callers.
async def test_security_cancel_token_during_fast_tool_is_not_success(tmp_path, monkeypatch):
    c = database(tmp_path / 'missions.db')
    try:
        add_mission(c)
        executor, _ = wired(monkeypatch, store(c))
        token = CancelToken()
        tool = FastTool(lambda: token.cancel('kill_switch'))
        trace = uuid4()
        await cancelled(executor.execute(tool, {}, config_snapshot=config(),
                                         trace_id=trace, cancel_token=token))
        assert c.execute('SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?',
                         (str(trace),)).fetchone() == ('cancelled',)
    finally:
        c.close()


async def test_security_lease_reader_failure_after_tool_fails_closed(tmp_path, monkeypatch):
    c = database(tmp_path / 'missions.db')
    try:
        add_mission(c)
        executor, _ = wired(monkeypatch, store(c))
        checks = 0

        async def lease_checker(*args):
            nonlocal checks
            checks += 1
            if checks > 1:
                raise sqlite3.OperationalError('post-result DB outage')
            return True

        monkeypatch.setattr(runtime_refs, 'get_mission_tool_execution_lease_checker',
                            lambda: lease_checker)
        tool = FastTool()
        trace = uuid4()
        await cancelled(executor.execute(tool, {}, config_snapshot=config(), trace_id=trace))
        assert checks >= 2 and tool.calls == 1
        assert c.execute('SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?',
                         (str(trace),)).fetchone() == ('cancelled',)
    finally:
        c.close()


async def test_security_unrelated_mission_cancel_does_not_deny(tmp_path, monkeypatch):
    c = database(tmp_path / 'missions.db')
    try:
        add_mission(c)
        c.execute("INSERT INTO missions (id,prompt,state,language,created_ms,updated_ms) "
                  "VALUES ('other','task','RUNNING','de',1,1)")
        executor, _ = wired(monkeypatch, store(c))

        def unrelated_cancel():
            c.execute("UPDATE missions SET state='CANCELLED' WHERE id='other'")

        tool = FastTool(unrelated_cancel)
        result = await executor.execute(tool, {}, config_snapshot=config(), trace_id=uuid4())
        assert result.success and tool.calls == 1
    finally:
        c.close()


async def test_security_nonmission_tool_still_runs_without_checker(monkeypatch):
    executor = await direct(monkeypatch)
    tool = FastTool()
    result = await executor.execute(tool, {}, config_snapshot={'tool_origin': 'chat'})
    assert result.success and tool.calls == 1

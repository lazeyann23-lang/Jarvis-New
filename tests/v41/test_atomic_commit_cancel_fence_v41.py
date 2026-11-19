"""V41: SQLite commit-time cancel latch beats stale post-result success.

Real production schema, two independent SQLite connections, and an actual
OS writer; the async SQLite adapter is only a test fixture (no aiosqlite).
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest

from jarvis.core import runtime_refs
from tests.v25.test_mission_execution_leases_v25 import (
    FakeTool, add_mission, config, database, store,
)
from tests.v31.test_running_execution_cancel_latch_v31 import wired

pytestmark = pytest.mark.asyncio


async def reserved(c):
    trace = str(uuid4())
    s = store(c)
    assert await s.try_start_tool_execution(
        trace_id=trace, mission_id='m1', tool_name='external-action',
    )
    assert await s.try_activate_tool_execution(
        trace_id=trace, mission_id='m1', tool_name='external-action',
    )
    return trace


def state(c, trace):
    return c.execute(
        'SELECT outcome, cancel_requested_ms FROM mission_tool_execution_leases WHERE trace_id=?',
        (trace,),
    ).fetchone()


# FUNCTION: the atomic UPDATE's result is the actual committed status.
async def test_function_completed_stays_completed_when_not_cancelled(tmp_path):
    c = database(tmp_path/'db')
    try:
        add_mission(c); trace = await reserved(c)
        assert await store(c).finish_tool_execution(trace_id=trace, outcome='completed') == 'completed'
        assert state(c, trace) == ('completed', None)
    finally:
        c.close()


async def test_function_cancel_latch_overrides_stale_completed(tmp_path):
    c = database(tmp_path/'db')
    try:
        add_mission(c); trace = await reserved(c)
        c.execute("UPDATE missions SET state='WAITING_CAPACITY' WHERE id='m1'")
        c.execute("UPDATE missions SET state='RUNNING' WHERE id='m1'")
        assert await store(c).finish_tool_execution(trace_id=trace, outcome='completed') == 'cancelled'
        assert state(c, trace)[0] == 'cancelled'
        assert state(c, trace)[1] is not None
    finally:
        c.close()


async def test_function_non_success_outcomes_remain_explicit(tmp_path):
    c = database(tmp_path/'db')
    try:
        add_mission(c); trace = await reserved(c)
        c.execute("UPDATE missions SET state='CANCELLED' WHERE id='m1'")
        assert await store(c).finish_tool_execution(trace_id=trace, outcome='failed') == 'failed'
    finally:
        c.close()


# INTEGRATION: the writer commits after the post-result watcher, before finisher.
async def test_integration_second_connection_cancel_at_finalizer(tmp_path, monkeypatch):
    path = tmp_path/'db'; a, b = database(path), database(path)
    try:
        add_mission(a); s = store(a)
        executor, denied = wired(monkeypatch, s)
        original = runtime_refs.get_mission_tool_execution_finisher()
        async def concurrent_finalizer(tid, outcome):
            b.execute("UPDATE missions SET state='WAITING_CAPACITY' WHERE id='m1'")
            b.execute("UPDATE missions SET state='RUNNING' WHERE id='m1'")
            return await original(tid, outcome)
        monkeypatch.setattr(runtime_refs, 'get_mission_tool_execution_finisher',
                            lambda: concurrent_finalizer)
        tool = FakeTool(); trace = uuid4()
        result = await executor.execute(tool, {}, config_snapshot=config(), trace_id=trace)
        assert tool.calls == 1 and not result.success
        assert result.error == 'approval-denied (mission_cancelled_during_execution)'
        assert state(b, str(trace))[0] == 'cancelled'
        assert any(event.reason == 'mission_cancelled_during_execution' for event in denied)
    finally:
        a.close(); b.close()


async def test_integration_independent_os_writer_commits_before_finalize(tmp_path, monkeypatch):
    path = tmp_path/'db'; a = database(path)
    try:
        add_mission(a); s = store(a)
        executor, _ = wired(monkeypatch, s)
        original = runtime_refs.get_mission_tool_execution_finisher()
        async def os_writer_finalizer(tid, outcome):
            code = ("import sqlite3,sys; c=sqlite3.connect(sys.argv[1],isolation_level=None); "
                    "c.execute(\"UPDATE missions SET state='WAITING_CAPACITY' WHERE id='m1'\"); "
                    "c.execute(\"UPDATE missions SET state='RUNNING' WHERE id='m1'\"); c.close()")
            subprocess.run([sys.executable, '-c', code, str(path)],
                           check=True, capture_output=True, timeout=6)
            return await original(tid, outcome)
        monkeypatch.setattr(runtime_refs, 'get_mission_tool_execution_finisher',
                            lambda: os_writer_finalizer)
        trace = uuid4(); tool = FakeTool()
        result = await executor.execute(tool, {}, config_snapshot=config(), trace_id=trace)
        assert not result.success and tool.calls == 1
        assert state(a, str(trace))[0] == 'cancelled'
    finally:
        a.close()


async def test_integration_cancel_after_commit_does_not_rewrite_completed(tmp_path):
    path = tmp_path/'db'; a, b = database(path), database(path)
    try:
        add_mission(a); trace = await reserved(a)
        assert await store(a).finish_tool_execution(trace_id=trace, outcome='completed') == 'completed'
        b.execute("UPDATE missions SET state='CANCELLED' WHERE id='m1'")
        assert state(a, trace) == ('completed', None)
    finally:
        a.close(); b.close()


# SECURITY: fail closed on finalizer failure, missing leases, and replay.
async def test_security_failed_finalization_never_returns_success(tmp_path, monkeypatch):
    c = database(tmp_path/'db')
    try:
        add_mission(c); executor, _ = wired(monkeypatch, store(c))
        async def broken(tid, outcome):
            raise sqlite3.OperationalError('commit unavailable')
        monkeypatch.setattr(runtime_refs, 'get_mission_tool_execution_finisher', lambda: broken)
        trace = uuid4(); tool = FakeTool()
        result = await executor.execute(tool, {}, config_snapshot=config(), trace_id=trace)
        assert not result.success
        assert result.error == 'approval-denied (mission_execution_finalization_failed)'
        assert state(c, str(trace))[0] == 'running'
    finally:
        c.close()


async def test_security_missing_or_double_finished_lease_is_error(tmp_path):
    c = database(tmp_path/'db')
    try:
        add_mission(c); s = store(c)
        with pytest.raises(RuntimeError):
            await s.finish_tool_execution(trace_id='missing', outcome='completed')
        trace = await reserved(c)
        assert await s.finish_tool_execution(trace_id=trace, outcome='completed') == 'completed'
        with pytest.raises(RuntimeError):
            await s.finish_tool_execution(trace_id=trace, outcome='completed')
    finally:
        c.close()


async def test_security_cancelled_trace_cannot_replay(tmp_path):
    c = database(tmp_path/'db')
    try:
        add_mission(c); s = store(c); trace = await reserved(c)
        c.execute("UPDATE missions SET state='WAITING_CAPACITY' WHERE id='m1'")
        c.execute("UPDATE missions SET state='RUNNING' WHERE id='m1'")
        assert await s.finish_tool_execution(trace_id=trace, outcome='completed') == 'cancelled'
        assert not await s.try_start_tool_execution(
            trace_id=trace, mission_id='m1', tool_name='external-action',
        )
    finally:
        c.close()


async def test_security_legacy_finisher_without_return_still_works(tmp_path, monkeypatch):
    c = database(tmp_path/'db')
    try:
        add_mission(c); s = store(c)
        executor, _ = wired(monkeypatch, s)
        original = runtime_refs.get_mission_tool_execution_finisher()
        async def old_callback(tid, outcome):
            await original(tid, outcome)
        monkeypatch.setattr(runtime_refs, 'get_mission_tool_execution_finisher', lambda: old_callback)
        tool = FakeTool()
        result = await executor.execute(tool, {}, config_snapshot=config(), trace_id=uuid4())
        assert result.success and tool.calls == 1
    finally:
        c.close()

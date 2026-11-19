"""V31: running mission tools must observe the persistent per-trace cancel latch."""
from __future__ import annotations
import asyncio
import sqlite3
import subprocess
import sys
from uuid import uuid4
import pytest
from jarvis.core import runtime_refs
from tests.v25.test_mission_execution_leases_v25 import (
    FakeTool, add_mission, config, database, setup, store,
)
pytestmark = pytest.mark.asyncio


def wired(monkeypatch, s):
    executor, denied = setup(monkeypatch, s)
    async def activate(tid, mission, tool):
        return await s.try_activate_tool_execution(trace_id=str(tid), mission_id=mission, tool_name=tool)
    async def lease_check(tid, mission, tool):
        return await s.is_execution_lease_uncancelled(trace_id=str(tid), mission_id=mission, tool_name=tool)
    monkeypatch.setattr(runtime_refs, 'get_mission_tool_execution_activator', lambda: activate)
    monkeypatch.setattr(runtime_refs, 'get_mission_tool_execution_lease_checker', lambda: lease_check)
    return executor, denied


async def blocked(executor):
    tool = FakeTool(); tool.block = True
    task = asyncio.create_task(executor.execute(tool, {}, config_snapshot=config(), trace_id=uuid4()))
    await asyncio.wait_for(tool.started.wait(), 2)
    return tool, task


async def reserve(s):
    trace = str(uuid4())
    assert await s.try_start_tool_execution(trace_id=trace, mission_id='m1', tool_name='external-action')
    assert await s.try_activate_tool_execution(trace_id=trace, mission_id='m1', tool_name='external-action')
    return trace


async def lease(s, trace, mission='m1', tool='external-action'):
    return await s.is_execution_lease_uncancelled(trace_id=trace, mission_id=mission, tool_name=tool)


# FUNCTION
async def test_function_active_lease_allows_execution(tmp_path):
    c = database(tmp_path/'m.db')
    try:
        add_mission(c); s = store(c); trace = await reserve(s)
        assert await lease(s, trace)
    finally: c.close()


async def test_function_pause_resume_latch_is_sticky(tmp_path):
    c = database(tmp_path/'m.db')
    try:
        add_mission(c); s = store(c); trace = await reserve(s)
        c.execute("UPDATE missions SET state='WAITING_CAPACITY' WHERE id='m1'")
        c.execute("UPDATE missions SET state='RUNNING' WHERE id='m1'")
        assert await s.is_active_for_tool_approval('m1')
        assert not await lease(s, trace)
    finally: c.close()


async def test_function_missing_wrong_finished_lease_fails_closed(tmp_path):
    c = database(tmp_path/'m.db')
    try:
        add_mission(c); s = store(c); trace = await reserve(s)
        assert not await lease(s, 'nonexistent')
        assert not await lease(s, trace, 'wrong')
        assert not await lease(s, trace, tool='wrong')
        assert not await lease(s, '')
        await s.finish_tool_execution(trace_id=trace, outcome='completed')
        assert not await lease(s, trace)
    finally: c.close()


# INTEGRATION
async def test_integration_two_connections_pause_resume_cancels_running_tool(tmp_path, monkeypatch):
    path = tmp_path/'m.db'; a, b = database(path), database(path)
    try:
        add_mission(a); executor, denied = wired(monkeypatch, store(a))
        tool, task = await blocked(executor)
        b.execute("UPDATE missions SET state='WAITING_CAPACITY' WHERE id='m1'")
        b.execute("UPDATE missions SET state='RUNNING' WHERE id='m1'")
        result = await asyncio.wait_for(task, 3)
        assert not result.success and tool.cancelled
        assert any(e.reason == 'mission_cancelled_during_execution' for e in denied)
        assert a.execute('SELECT outcome FROM mission_tool_execution_leases').fetchone()[0] == 'cancelled'
    finally: a.close(); b.close()


async def test_integration_separate_process_pause_resume_cancels_running_tool(tmp_path, monkeypatch):
    path = tmp_path/'m.db'; c = database(path)
    try:
        add_mission(c); executor, _ = wired(monkeypatch, store(c))
        tool, task = await blocked(executor)
        code = ("import sqlite3,sys; c=sqlite3.connect(sys.argv[1], isolation_level=None); "
                "c.execute(\"UPDATE missions SET state='WAITING_CAPACITY' WHERE id='m1'\"); "
                "c.execute(\"UPDATE missions SET state='RUNNING' WHERE id='m1'\"); c.close()")
        await asyncio.to_thread(subprocess.run, [sys.executable, '-c', code, str(path)],
                                check=True, capture_output=True, timeout=6)
        result = await asyncio.wait_for(task, 3)
        assert not result.success and tool.cancelled
    finally: c.close()


async def test_integration_eventlog_pause_resume_cancels_running_tool(tmp_path, monkeypatch):
    path = tmp_path/'m.db'; a, b = database(path), database(path)
    try:
        add_mission(a); executor, _ = wired(monkeypatch, store(a))
        tool, task = await blocked(executor)
        for state in ('WAITING_CAPACITY', 'RUNNING'):
            b.execute("INSERT INTO mission_events (event_id,mission_id,event_type,source_actor,ts_ms,payload_json) "
                      "VALUES (?, 'm1', 'MissionStateChanged', 'ui', 2, ?)",
                      (str(uuid4()), '{"to_state":"' + state + '"}'))
        assert await store(a).is_active_for_tool_approval('m1')
        result = await asyncio.wait_for(task, 3)
        assert not result.success and tool.cancelled
    finally: a.close(); b.close()


# SECURITY / REGRESSION
async def test_security_db_failure_in_latch_reader_cancels_running_tool(tmp_path, monkeypatch):
    c = database(tmp_path/'m.db')
    try:
        add_mission(c); executor, _ = wired(monkeypatch, store(c))
        calls = 0
        async def fails_after_preflight(tid, mission, tool):
            nonlocal calls
            calls += 1
            if calls > 1: raise sqlite3.OperationalError('DB unavailable')
            return True
        monkeypatch.setattr(runtime_refs, 'get_mission_tool_execution_lease_checker',
                            lambda: fails_after_preflight)
        tool, task = await blocked(executor)
        result = await asyncio.wait_for(task, 3)
        assert not result.success and tool.cancelled
    finally: c.close()


async def test_security_preflight_denies_without_tool_side_effect(tmp_path, monkeypatch):
    c = database(tmp_path/'m.db')
    try:
        add_mission(c); executor, _ = wired(monkeypatch, store(c))
        async def denied(tid, mission, tool): return False
        monkeypatch.setattr(runtime_refs, 'get_mission_tool_execution_lease_checker', lambda: denied)
        tool = FakeTool()
        result = await executor.execute(tool, {}, config_snapshot=config(), trace_id=uuid4())
        assert not result.success and tool.calls == 0
    finally: c.close()


async def test_security_unrelated_mission_cancel_does_not_stop_tool(tmp_path, monkeypatch):
    c = database(tmp_path/'m.db')
    try:
        add_mission(c); executor, _ = wired(monkeypatch, store(c))
        c.execute("INSERT INTO missions (id,prompt,state,language,created_ms,updated_ms) "
                  "VALUES ('other','task','RUNNING','de',1,1)")
        c.execute("UPDATE missions SET state='CANCELLED' WHERE id='other'")
        tool = FakeTool()
        result = await executor.execute(tool, {}, config_snapshot=config(), trace_id=uuid4())
        assert result.success and tool.calls == 1
    finally: c.close()


async def test_security_legacy_callback_absent_keeps_v28_behavior(tmp_path, monkeypatch):
    c = database(tmp_path/'m.db')
    try:
        add_mission(c); executor, _ = wired(monkeypatch, store(c))
        monkeypatch.setattr(runtime_refs, 'get_mission_tool_execution_lease_checker', lambda: None)
        tool = FakeTool()
        result = await executor.execute(tool, {}, config_snapshot=config(), trace_id=uuid4())
        assert result.success and tool.calls == 1
    finally: c.close()

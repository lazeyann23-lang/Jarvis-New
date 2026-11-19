"""V43: a second durable writer fence runs *inside* the scheduled work task.

Function, integration, and security tests use real SQLite and the test-only
stdlib async adapter. No real user tools or irreversible actions are invoked.
"""
from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from jarvis.control.cancel import CancelToken
from jarvis.core import runtime_refs
from tests.v25.test_mission_execution_leases_v25 import (
    FakeTool, add_mission, config, database, store, setup,
)
from tests.v42.test_dispatch_writer_cancel_fence_v42 import wire_fence, lease_outcome

pytestmark = pytest.mark.asyncio


def cancel_in(conn, state='WAITING_CAPACITY'):
    conn.execute('UPDATE missions SET state=? WHERE id=?', (state, 'm1'))


def denied(result):
    assert not result.success
    assert result.error == 'approval-denied (mission_cancelled_during_execution)'


# FUNCTION: the production callback is checked both before scheduling and at
# the first instruction of the scheduled work task.
async def test_function_two_writer_checks_on_success(tmp_path, monkeypatch):
    c = database(tmp_path/'m.db')
    try:
        add_mission(c); s = store(c); calls = []
        async def fence(tid, mission, name):
            calls.append(str(tid))
            return await s.try_fence_tool_dispatch(
                trace_id=str(tid), mission_id=mission, tool_name=name)
        executor, _ = wire_fence(monkeypatch, s, fence)
        tool = FakeTool(); trace = uuid4()
        result = await executor.execute(tool, {}, config_snapshot=config(), trace_id=trace)
        assert result.success and tool.calls == 1
        assert calls == [str(trace), str(trace)]
        assert lease_outcome(c, trace) == 'completed'
    finally:
        c.close()


async def test_function_cancel_in_scheduling_gap_blocks_tool(tmp_path, monkeypatch):
    c = database(tmp_path/'m.db')
    try:
        add_mission(c); s = store(c); calls = 0
        async def fence(tid, mission, name):
            nonlocal calls
            calls += 1
            allowed = await s.try_fence_tool_dispatch(
                trace_id=str(tid), mission_id=mission, tool_name=name)
            if calls == 1:
                # Runs on the next loop turn, before the newly scheduled work.
                asyncio.get_running_loop().call_soon(cancel_in, c)
            return allowed
        executor, _ = wire_fence(monkeypatch, s, fence)
        tool = FakeTool(); trace = uuid4()
        result = await executor.execute(tool, {}, config_snapshot=config(), trace_id=trace)
        denied(result)
        assert tool.calls == 0 and calls == 2
        assert lease_outcome(c, trace) == 'cancelled'
    finally:
        c.close()


async def test_function_legacy_no_writer_callback_unchanged(tmp_path, monkeypatch):
    c = database(tmp_path/'m.db')
    try:
        add_mission(c); s = store(c)
        executor, _ = wire_fence(monkeypatch, s)
        monkeypatch.setattr(runtime_refs, 'get_mission_tool_execution_dispatch_fence', lambda: None)
        tool = FakeTool()
        result = await executor.execute(tool, {}, config_snapshot=config())
        assert result.success and tool.calls == 1
    finally:
        c.close()


# INTEGRATION: a second connection or OS process can commit cancellation in
# the gap between V42's outer fence and the work coroutine's first instruction.
async def test_integration_second_connection_cancel_in_gap(tmp_path, monkeypatch):
    path = tmp_path/'m.db'; a = database(path); b = database(path)
    try:
        add_mission(a); s = store(a); calls = 0
        async def fence(tid, mission, name):
            nonlocal calls
            calls += 1
            allowed = await s.try_fence_tool_dispatch(
                trace_id=str(tid), mission_id=mission, tool_name=name)
            if calls == 1:
                asyncio.get_running_loop().call_soon(cancel_in, b)
            return allowed
        executor, _ = wire_fence(monkeypatch, s, fence)
        tool = FakeTool(); trace = uuid4()
        denied(await executor.execute(tool, {}, config_snapshot=config(), trace_id=trace))
        assert calls == 2 and tool.calls == 0
        assert lease_outcome(b, trace) == 'cancelled'
    finally:
        a.close(); b.close()


async def test_integration_separate_os_cancel_writer_after_first_fence(tmp_path, monkeypatch):
    path = tmp_path/'m.db'; a = database(path); proc = None
    try:
        add_mission(a); s = store(a); calls = 0
        ready = tmp_path/'ready'
        code = (
            'import sqlite3,sys,time,pathlib; '
            'c=sqlite3.connect(sys.argv[1],isolation_level=None,timeout=5); '
            "c.execute('BEGIN IMMEDIATE'); "
            "c.execute(\"UPDATE missions SET state='WAITING_CAPACITY' WHERE id='m1'\"); "
            'pathlib.Path(sys.argv[2]).write_text("locked"); '
            'time.sleep(0.2); c.commit(); c.close()'
        )
        async def fence(tid, mission, name):
            nonlocal calls, proc
            calls += 1
            allowed = await s.try_fence_tool_dispatch(
                trace_id=str(tid), mission_id=mission, tool_name=name)
            if calls == 1:
                proc = subprocess.Popen([sys.executable, '-c', code, str(path), str(ready)])
                deadline = time.monotonic() + 4
                while not ready.exists() and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                assert ready.exists(), 'OS cancellation writer failed to lock'
            return allowed
        executor, _ = wire_fence(monkeypatch, s, fence)
        tool = FakeTool(); trace = uuid4()
        denied(await executor.execute(tool, {}, config_snapshot=config(), trace_id=trace))
        assert calls == 2 and tool.calls == 0
        assert lease_outcome(a, trace) == 'cancelled'
        assert proc is not None and proc.wait(timeout=3) == 0
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill(); proc.wait(timeout=3)
        a.close()


async def test_integration_unrelated_writer_does_not_block_dispatch(tmp_path, monkeypatch):
    path = tmp_path/'m.db'; a = database(path); b = database(path)
    try:
        add_mission(a); s = store(a); calls = 0
        async def fence(tid, mission, name):
            nonlocal calls
            calls += 1
            allowed = await s.try_fence_tool_dispatch(
                trace_id=str(tid), mission_id=mission, tool_name=name)
            if calls == 1:
                asyncio.get_running_loop().call_soon(
                    b.execute, 'UPDATE missions SET updated_ms=updated_ms+1 WHERE id=?', ('m1',))
            return allowed
        executor, _ = wire_fence(monkeypatch, s, fence)
        tool = FakeTool(); trace = uuid4()
        result = await executor.execute(tool, {}, config_snapshot=config(), trace_id=trace)
        assert result.success and tool.calls == 1 and calls == 2
        assert lease_outcome(a, trace) == 'completed'
    finally:
        a.close(); b.close()


# SECURITY / REGRESSION: errors, refusals and token cancellation are fail-closed.
async def test_security_second_writer_check_error_denies(tmp_path, monkeypatch):
    c = database(tmp_path/'m.db')
    try:
        add_mission(c); s = store(c); calls = 0
        async def fence(tid, mission, name):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise sqlite3.OperationalError('DB unavailable after scheduling')
            return await s.try_fence_tool_dispatch(
                trace_id=str(tid), mission_id=mission, tool_name=name)
        executor, _ = wire_fence(monkeypatch, s, fence)
        tool = FakeTool(); trace = uuid4()
        denied(await executor.execute(tool, {}, config_snapshot=config(), trace_id=trace))
        assert calls == 2 and tool.calls == 0
        assert lease_outcome(c, trace) == 'cancelled'
    finally:
        c.close()


async def test_security_second_writer_refusal_denies(tmp_path, monkeypatch):
    c = database(tmp_path/'m.db')
    try:
        add_mission(c); s = store(c); calls = 0
        async def fence(tid, mission, name):
            nonlocal calls
            calls += 1
            if calls == 2:
                return False
            return await s.try_fence_tool_dispatch(
                trace_id=str(tid), mission_id=mission, tool_name=name)
        executor, _ = wire_fence(monkeypatch, s, fence)
        tool = FakeTool(); trace = uuid4()
        denied(await executor.execute(tool, {}, config_snapshot=config(), trace_id=trace))
        assert tool.calls == 0 and lease_outcome(c, trace) == 'cancelled'
    finally:
        c.close()


async def test_security_token_cancel_in_scheduling_gap(tmp_path, monkeypatch):
    c = database(tmp_path/'m.db')
    try:
        add_mission(c); s = store(c); token = CancelToken(); calls = 0
        async def fence(tid, mission, name):
            nonlocal calls
            calls += 1
            allowed = await s.try_fence_tool_dispatch(
                trace_id=str(tid), mission_id=mission, tool_name=name)
            if calls == 1:
                asyncio.get_running_loop().call_soon(token.cancel, 'user')
            return allowed
        executor, _ = wire_fence(monkeypatch, s, fence)
        tool = FakeTool(); trace = uuid4()
        denied(await executor.execute(tool, {}, config_snapshot=config(), trace_id=trace,
                                      cancel_token=token))
        assert tool.calls == 0 and lease_outcome(c, trace) == 'cancelled'
    finally:
        c.close()


async def test_security_chat_origin_does_not_touch_mission_fence(tmp_path, monkeypatch):
    c = database(tmp_path/'m.db')
    try:
        add_mission(c); s = store(c); calls = 0
        async def fence(tid, mission, name):
            nonlocal calls
            calls += 1
            raise AssertionError('chat must not use mission dispatch fence')
        executor, _ = wire_fence(monkeypatch, s, fence)
        tool = FakeTool()
        result = await executor.execute(tool, {}, config_snapshot={'tool_origin': 'chat'})
        assert result.success and tool.calls == 1 and calls == 0
    finally:
        c.close()

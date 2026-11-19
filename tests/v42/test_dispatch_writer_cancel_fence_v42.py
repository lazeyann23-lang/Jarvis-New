"""V42: a final SQLite writer fence waits for pending cross-process cancel.

Function / integration / safety groups use production MissionEventStore SQL with
real SQLite and the test-only stdlib async adapter. No external tool side effects.
"""
from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
import threading
import time
from uuid import uuid4

import pytest

from jarvis.control.cancel import CancelToken
from jarvis.core import runtime_refs
from tests.v25.test_mission_execution_leases_v25 import (
    FakeTool, add_mission, config, database, store,
)
from tests.v31.test_running_execution_cancel_latch_v31 import wired

pytestmark = pytest.mark.asyncio


async def reserve(s):
    trace = str(uuid4())
    assert await s.try_start_tool_execution(
        trace_id=trace, mission_id='m1', tool_name='external-action')
    assert await s.try_activate_tool_execution(
        trace_id=trace, mission_id='m1', tool_name='external-action')
    return trace


def wire_fence(monkeypatch, s, callback=None):
    executor, denied = wired(monkeypatch, s)
    async def real(tid, mission, tool):
        return await s.try_fence_tool_dispatch(
            trace_id=str(tid), mission_id=mission, tool_name=tool)
    monkeypatch.setattr(runtime_refs, 'get_mission_tool_execution_dispatch_fence',
                        lambda: callback or real)
    return executor, denied


def lease_outcome(conn, trace):
    return conn.execute('SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?',
                        (str(trace),)).fetchone()[0]


# FUNCTION: a writer-serialized permit requires an active, activated trace.
async def test_function_active_activated_trace_permitted(tmp_path):
    c = database(tmp_path/'m.db')
    try:
        add_mission(c); s = store(c); trace = await reserve(s)
        assert await s.try_fence_tool_dispatch(
            trace_id=trace, mission_id='m1', tool_name='external-action')
        assert lease_outcome(c, trace) == 'running'
    finally:
        c.close()


async def test_function_claimed_but_unactivated_trace_refused(tmp_path):
    c = database(tmp_path/'m.db')
    try:
        add_mission(c); s = store(c); trace = str(uuid4())
        assert await s.try_start_tool_execution(
            trace_id=trace, mission_id='m1', tool_name='external-action')
        assert not await s.try_fence_tool_dispatch(
            trace_id=trace, mission_id='m1', tool_name='external-action')
    finally:
        c.close()


async def test_function_pause_resume_sticky_latch_refuses_dispatch(tmp_path):
    c = database(tmp_path/'m.db')
    try:
        add_mission(c); s = store(c); trace = await reserve(s)
        c.execute("UPDATE missions SET state='WAITING_CAPACITY' WHERE id='m1'")
        c.execute("UPDATE missions SET state='RUNNING' WHERE id='m1'")
        assert not await s.try_fence_tool_dispatch(
            trace_id=trace, mission_id='m1', tool_name='external-action')
    finally:
        c.close()


# INTEGRATION: writer lock prevents dispatch on stale preflight snapshot.
async def test_integration_two_connections_pending_cancel_writer(tmp_path):
    path = tmp_path/'m.db'; a = database(path)
    b = sqlite3.connect(path, isolation_level=None, timeout=5, check_same_thread=False)
    try:
        add_mission(a); s = store(a); trace = await reserve(s)
        b.execute('BEGIN IMMEDIATE')
        b.execute("UPDATE missions SET state='WAITING_CAPACITY' WHERE id='m1'")
        # The old snapshot is still visible to the read-only preflight.
        assert await s.is_execution_lease_uncancelled(
            trace_id=trace, mission_id='m1', tool_name='external-action')
        timer = threading.Timer(0.2, b.commit)
        timer.start()
        try:
            assert not await s.try_fence_tool_dispatch(
                trace_id=trace, mission_id='m1', tool_name='external-action')
        finally:
            timer.join(timeout=2)
        assert a.execute('SELECT cancel_requested_ms FROM mission_tool_execution_leases '
                         'WHERE trace_id=?', (trace,)).fetchone()[0] is not None
    finally:
        if b.in_transaction: b.rollback()
        a.close(); b.close()


async def test_integration_independent_os_cancel_writer_blocks_tool(tmp_path, monkeypatch):
    path = tmp_path/'m.db'; a = database(path)
    try:
        add_mission(a); s = store(a)
        process = None
        # Inject the competing writer immediately AFTER the existing read-only
        # preflight. V41 would schedule tool.execute while this writer was
        # uncommitted; V42's last writer gate waits and refuses dispatch.
        async def preflight_with_inflight_writer(tid, mission, tool):
            nonlocal process
            allowed = await s.is_execution_lease_uncancelled(
                trace_id=str(tid), mission_id=mission, tool_name=tool)
            ready = tmp_path/'writer_ready'
            code = (
                "import sqlite3,sys,time,pathlib; "
                "c=sqlite3.connect(sys.argv[1],isolation_level=None); "
                "c.execute('BEGIN IMMEDIATE'); "
                "c.execute(\"UPDATE missions SET state='WAITING_CAPACITY' WHERE id='m1'\"); "
                "pathlib.Path(sys.argv[2]).write_text('ready'); "
                "time.sleep(0.3); c.commit(); c.close()"
            )
            process = subprocess.Popen([sys.executable, '-c', code, str(path), str(ready)])
            deadline = time.monotonic() + 4
            while not ready.exists() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert ready.exists(), 'independent writer did not acquire lock'
            return allowed
        executor, _ = wire_fence(monkeypatch, s)
        monkeypatch.setattr(runtime_refs, 'get_mission_tool_execution_lease_checker',
                            lambda: preflight_with_inflight_writer)
        tool = FakeTool(); trace = uuid4()
        result = await executor.execute(tool, {}, config_snapshot=config(), trace_id=trace)
        assert not result.success and tool.calls == 0
        assert result.error == 'approval-denied (mission_cancelled_during_execution)'
        assert lease_outcome(a, trace) == 'cancelled'
        assert process is not None and process.wait(timeout=3) == 0
    finally:
        if process is not None and process.poll() is None:
            process.kill(); process.wait(timeout=3)
        a.close()


async def test_integration_unrelated_pending_writer_allows_dispatch(tmp_path, monkeypatch):
    path = tmp_path/'m.db'; a = database(path)
    b = sqlite3.connect(path, isolation_level=None, timeout=5, check_same_thread=False)
    try:
        add_mission(a); s = store(a)
        async def fence(tid, mission, tool):
            b.execute('BEGIN IMMEDIATE')
            b.execute("UPDATE missions SET updated_ms=updated_ms+1 WHERE id='m1'")
            timer = threading.Timer(0.15, b.commit)
            timer.start()
            try:
                return await s.try_fence_tool_dispatch(
                    trace_id=str(tid), mission_id=mission, tool_name=tool)
            finally:
                timer.join(timeout=2)
        executor, _ = wire_fence(monkeypatch, s, fence)
        tool = FakeTool(); trace = uuid4()
        result = await executor.execute(tool, {}, config_snapshot=config(), trace_id=trace)
        assert result.success and tool.calls == 1 and lease_outcome(a, trace) == 'completed'
    finally:
        if b.in_transaction: b.rollback()
        a.close(); b.close()


# SECURITY: wrong/missing lease, DB failure, token, legacy compatibility.
async def test_security_wrong_trace_mission_tool_and_finished_refused(tmp_path):
    c = database(tmp_path/'m.db')
    try:
        add_mission(c); s = store(c); trace = await reserve(s)
        for tid, mid, tool in [('', 'm1', 'external-action'),
                               ('missing', 'm1', 'external-action'),
                               (trace, 'wrong', 'external-action'),
                               (trace, 'm1', 'wrong')]:
            assert not await s.try_fence_tool_dispatch(
                trace_id=tid, mission_id=mid, tool_name=tool)
        await s.finish_tool_execution(trace_id=trace, outcome='completed')
        assert not await s.try_fence_tool_dispatch(
            trace_id=trace, mission_id='m1', tool_name='external-action')
    finally:
        c.close()


async def test_security_dispatch_database_error_fails_closed(tmp_path, monkeypatch):
    c = database(tmp_path/'m.db')
    try:
        add_mission(c); s = store(c)
        async def broken(tid, mission, tool):
            raise sqlite3.OperationalError('writer unavailable')
        executor, _ = wire_fence(monkeypatch, s, broken)
        tool = FakeTool(); trace = uuid4()
        result = await executor.execute(tool, {}, config_snapshot=config(), trace_id=trace)
        assert not result.success and tool.calls == 0
        assert lease_outcome(c, trace) == 'cancelled'
    finally:
        c.close()


async def test_security_token_cancelled_during_writer_fence_denies(tmp_path, monkeypatch):
    c = database(tmp_path/'m.db')
    try:
        add_mission(c); s = store(c); token = CancelToken()
        async def token_during_fence(tid, mission, tool):
            permitted = await s.try_fence_tool_dispatch(
                trace_id=str(tid), mission_id=mission, tool_name=tool)
            token.cancel('user')
            return permitted
        executor, _ = wire_fence(monkeypatch, s, token_during_fence)
        tool = FakeTool(); trace = uuid4()
        result = await executor.execute(tool, {}, config_snapshot=config(),
                                        trace_id=trace, cancel_token=token)
        assert not result.success and tool.calls == 0
        assert lease_outcome(c, trace) == 'cancelled'
    finally:
        c.close()


async def test_security_legacy_without_writer_callback_unchanged(tmp_path, monkeypatch):
    c = database(tmp_path/'m.db')
    try:
        add_mission(c); s = store(c)
        executor, _ = wired(monkeypatch, s)
        monkeypatch.setattr(runtime_refs, 'get_mission_tool_execution_dispatch_fence',
                            lambda: None)
        tool = FakeTool(); trace = uuid4()
        result = await executor.execute(tool, {}, config_snapshot=config(), trace_id=trace)
        assert result.success and tool.calls == 1
        assert lease_outcome(c, trace) == 'completed'
    finally:
        c.close()

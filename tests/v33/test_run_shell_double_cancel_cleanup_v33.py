"""V33: a second cancellation cannot orphan an already-spawned shell.

The tests use only harmless local heartbeat subprocesses. POSIX process-tree
integration is skipped on Windows, which has not been tested on real hardware.
"""
from __future__ import annotations

import asyncio
import sys
from uuid import uuid4

import pytest

from jarvis.plugins.tool import run_shell
from jarvis.plugins.tool.run_shell import RunShellTool
from tests.v27.test_run_shell_cancel_tree_v27 import (
    SafeShell, assert_stopped, database, executor_with_store,
    heartbeat_command, mission, state_event, wait_for_file,
)

pytestmark = pytest.mark.asyncio
posix = pytest.mark.skipif(sys.platform == 'win32', reason='requires POSIX process groups')


class FakeProcess:
    pid = 123
    returncode = None

    async def communicate(self):
        raise OSError('pipe transport failure')


# FUNCTION (3)
async def test_function_tracked_cleanup_completes(monkeypatch):
    calls = []
    async def stop(proc): calls.append(proc)
    monkeypatch.setattr(run_shell, '_stop_shell_process', stop)
    proc = FakeProcess()
    await run_shell._stop_started_shell_process(proc)
    await asyncio.sleep(0)
    assert calls == [proc]
    assert not run_shell._PENDING_ACTIVE_CLEANUPS


async def test_function_double_cancel_retains_cleanup(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    completed = []
    async def stop(proc):
        entered.set()
        await release.wait()
        completed.append(proc)
    monkeypatch.setattr(run_shell, '_stop_shell_process', stop)
    proc = FakeProcess()
    caller = asyncio.create_task(run_shell._stop_started_shell_process(proc))
    await asyncio.wait_for(entered.wait(), 2)
    caller.cancel()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert len(run_shell._PENDING_ACTIVE_CLEANUPS) == 1
    release.set()
    for _ in range(100):
        if not run_shell._PENDING_ACTIVE_CLEANUPS: break
        await asyncio.sleep(.01)
    assert completed == [proc]
    assert not run_shell._PENDING_ACTIVE_CLEANUPS


async def test_function_communication_failure_reaps_process(monkeypatch):
    proc = FakeProcess()
    cleaned = []
    async def spawn(*a, **kw): return proc
    async def stop(p): cleaned.append(p)
    monkeypatch.setattr(run_shell, '_spawn_shell_process', spawn)
    monkeypatch.setattr(run_shell, '_stop_shell_process', stop)
    result = await RunShellTool().execute({'command': 'safe'}, None)
    assert not result.success and 'pipe transport failure' in result.error
    assert cleaned == [proc]
    await asyncio.sleep(0)
    assert not run_shell._PENDING_ACTIVE_CLEANUPS


# INTEGRATION (2)
@posix
async def test_integration_double_cancel_stops_real_process_tree(tmp_path, monkeypatch):
    command, marker = heartbeat_command(tmp_path)
    original = run_shell._stop_shell_process
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed_stop(proc):
        entered.set()
        await release.wait()
        await original(proc)
    monkeypatch.setattr(run_shell, '_stop_shell_process', delayed_stop)
    task = asyncio.create_task(RunShellTool().execute({'command': command, 'timeout_s': 15}, None))
    try:
        await wait_for_file(marker)
        task.cancel()
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()  # cancel again while OS cleanup is waiting
        with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(task, 3)
        assert run_shell._PENDING_ACTIVE_CLEANUPS
        release.set()
        for _ in range(200):
            if not run_shell._PENDING_ACTIVE_CLEANUPS: break
            await asyncio.sleep(.02)
        assert not run_shell._PENDING_ACTIVE_CLEANUPS
        await assert_stopped(marker)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # ensure the process cleanup finishes even on assertion failure
        if run_shell._PENDING_ACTIVE_CLEANUPS:
            await asyncio.gather(*run_shell._PENDING_ACTIVE_CLEANUPS, return_exceptions=True)


@posix
async def test_integration_sqlite_cancel_stops_real_process(tmp_path, monkeypatch):
    a = database(tmp_path / 'missions.db')
    b = database(tmp_path / 'missions.db')
    mission(a)
    executor = executor_with_store(monkeypatch, a)
    command, marker = heartbeat_command(tmp_path)
    trace = uuid4()
    task = asyncio.create_task(executor.execute(
        SafeShell(), {'command': command, 'timeout_s': 12},
        config_snapshot={'tool_origin': 'mission_worker', 'mission_id': 'm1'},
        trace_id=trace,
    ))
    try:
        await wait_for_file(marker)
        state_event(b, 'CANCELLED')
        result = await asyncio.wait_for(task, 5)
        assert not result.success and 'mission_cancelled_during_execution' in result.error
        assert a.execute('SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?',
                         (str(trace),)).fetchone() == ('cancelled',)
        await assert_stopped(marker)
        assert not run_shell._PENDING_ACTIVE_CLEANUPS
    finally:
        if not task.done():
            task.cancel(); await asyncio.gather(task, return_exceptions=True)
        a.close(); b.close()


# SECURITY / REGRESSION (4)
async def test_security_second_cancel_in_execute_keeps_reaper(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    completed = []
    async def stop(proc):
        entered.set(); await release.wait(); completed.append(proc)
    async def spawn(*a, **kw): return FakeProcess()
    monkeypatch.setattr(run_shell, '_spawn_shell_process', spawn)
    monkeypatch.setattr(run_shell, '_stop_shell_process', stop)
    task = asyncio.create_task(RunShellTool().execute({'command': 'safe'}, None))
    try:
        await asyncio.sleep(.02)
        task.cancel()
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert run_shell._PENDING_ACTIVE_CLEANUPS
        release.set()
        for _ in range(100):
            if not run_shell._PENDING_ACTIVE_CLEANUPS: break
            await asyncio.sleep(.01)
        assert len(completed) == 1 and not run_shell._PENDING_ACTIVE_CLEANUPS
    finally:
        release.set()


async def test_security_timeout_cleanup_survives_second_cancel(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    completed = []
    class SlowProcess:
        returncode = None
        async def communicate(self): await asyncio.sleep(30)
    async def spawn(*a, **kw): return SlowProcess()
    async def stop(proc):
        entered.set(); await release.wait(); completed.append(proc)
    monkeypatch.setattr(run_shell, '_spawn_shell_process', spawn)
    monkeypatch.setattr(run_shell, '_stop_shell_process', stop)
    task = asyncio.create_task(RunShellTool().execute({'command': 'safe', 'timeout_s': .01}, None))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert run_shell._PENDING_ACTIVE_CLEANUPS
        release.set()
        for _ in range(100):
            if not run_shell._PENDING_ACTIVE_CLEANUPS: break
            await asyncio.sleep(.01)
        assert len(completed) == 1 and not run_shell._PENDING_ACTIVE_CLEANUPS
    finally:
        release.set()


async def test_security_cleanup_failure_does_not_leave_pending_task(monkeypatch):
    async def stop(proc): raise RuntimeError('cleanup failed')
    monkeypatch.setattr(run_shell, '_stop_shell_process', stop)
    with pytest.raises(RuntimeError, match='cleanup failed'):
        await run_shell._stop_started_shell_process(FakeProcess())
    await asyncio.sleep(0)
    assert not run_shell._PENDING_ACTIVE_CLEANUPS


@posix
async def test_security_normal_success_does_not_start_cleanup():
    result = await RunShellTool().execute({'command': 'printf SAFE'}, None)
    assert result.success and result.output['stdout'] == 'SAFE'
    assert not run_shell._PENDING_ACTIVE_CLEANUPS

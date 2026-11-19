"""V34: do not finalize mission execution lease before post-cancel OS cleanup.

POSIX tests run harmless local heartbeat processes; Windows E2E is not claimed.
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
posix = pytest.mark.skipif(sys.platform == 'win32', reason='POSIX process group required')

class FakeProcess:
    pid = 1
    returncode = None

    async def communicate(self):
        await asyncio.sleep(60)


async def _fake_cancelled_tool(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    async def spawn(*args, **kwargs): return FakeProcess()
    async def stop(proc):
        entered.set()
        await release.wait()
    monkeypatch.setattr(run_shell, '_spawn_shell_process', spawn)
    monkeypatch.setattr(run_shell, '_stop_shell_process', stop)
    task = asyncio.create_task(RunShellTool().execute({'command': 'safe'}, None))
    await asyncio.sleep(.01)
    task.cancel()
    await asyncio.wait_for(entered.wait(), 2)
    return task, release


# FUNCTION (3)
async def test_function_cleanup_owned_by_tool_task(monkeypatch):
    task, release = await _fake_cancelled_tool(monkeypatch)
    try:
        assert RunShellTool.pending_cleanup_for_task(task) is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert RunShellTool.pending_cleanup_for_task(task) is not None
    finally:
        release.set()
        await asyncio.gather(*run_shell._PENDING_ACTIVE_CLEANUPS, return_exceptions=True)
    await asyncio.sleep(0)
    assert RunShellTool.pending_cleanup_for_task(task) is None


async def test_function_mapping_removed_after_cleanup(monkeypatch):
    task, release = await _fake_cancelled_tool(monkeypatch)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    release.set()
    await asyncio.gather(*run_shell._PENDING_ACTIVE_CLEANUPS, return_exceptions=True)
    await asyncio.sleep(0)
    assert task not in run_shell._TOOL_TASK_CLEANUPS


async def test_function_cleanup_tracking_is_per_invocation(monkeypatch):
    a, release = await _fake_cancelled_tool(monkeypatch)
    b = asyncio.create_task(asyncio.sleep(0))
    await b
    assert RunShellTool.pending_cleanup_for_task(a) is not None
    assert RunShellTool.pending_cleanup_for_task(b) is None
    a.cancel()
    with pytest.raises(asyncio.CancelledError): await a
    release.set()
    await asyncio.gather(*run_shell._PENDING_ACTIVE_CLEANUPS, return_exceptions=True)


# INTEGRATION (2)
@posix
async def test_integration_real_child_cleanup_after_double_cancel(tmp_path, monkeypatch):
    command, marker = heartbeat_command(tmp_path)
    original = run_shell._stop_shell_process
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed(proc):
        entered.set()
        await release.wait()
        await original(proc)
    monkeypatch.setattr(run_shell, '_stop_shell_process', delayed)
    task = asyncio.create_task(RunShellTool().execute({'command': command, 'timeout_s': 15}, None))
    try:
        await wait_for_file(marker)
        task.cancel()
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert RunShellTool.pending_cleanup_for_task(task) is not None
    finally:
        release.set()
        await asyncio.gather(*run_shell._PENDING_ACTIVE_CLEANUPS, return_exceptions=True)
    await assert_stopped(marker)
    assert RunShellTool.pending_cleanup_for_task(task) is None


@posix
async def test_integration_sqlite_lease_stays_running_until_os_cleanup(tmp_path, monkeypatch):
    a = database(tmp_path / 'missions.db')
    b = database(tmp_path / 'missions.db')
    mission(a)
    executor = executor_with_store(monkeypatch, a)
    command, marker = heartbeat_command(tmp_path)
    original = run_shell._stop_shell_process
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed(proc):
        entered.set()
        await release.wait()
        await original(proc)
    monkeypatch.setattr(run_shell, '_stop_shell_process', delayed)
    trace = uuid4()
    task = asyncio.create_task(executor.execute(
        SafeShell(), {'command': command, 'timeout_s': 12},
        config_snapshot={'tool_origin': 'mission_worker', 'mission_id': 'm1'},
        trace_id=trace,
    ))
    try:
        await wait_for_file(marker)
        state_event(b, 'CANCELLED')
        await asyncio.wait_for(entered.wait(), 3)
        owners = [t for t in list(run_shell._TOOL_TASK_CLEANUPS) if not t.done()]
        assert len(owners) == 1
        owners[0].cancel()  # second cancellation before OS process is reaped
        result = await asyncio.wait_for(task, 5)
        assert not result.success
        assert a.execute('SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?',
                         (str(trace),)).fetchone() == ('running',)
        # A replay is still forbidden even while the OS cleanup is pending.
        assert a.execute('SELECT count(*) FROM mission_tool_execution_leases WHERE trace_id=?',
                         (str(trace),)).fetchone() == (1,)
        release.set()
        for _ in range(200):
            if a.execute('SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?',
                         (str(trace),)).fetchone() == ('cancelled',): break
            await asyncio.sleep(.02)
        assert a.execute('SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?',
                         (str(trace),)).fetchone() == ('cancelled',)
        await assert_stopped(marker)
    finally:
        release.set()
        if not task.done(): task.cancel(); await asyncio.gather(task, return_exceptions=True)
        await asyncio.gather(*run_shell._PENDING_ACTIVE_CLEANUPS, return_exceptions=True)
        a.close(); b.close()


# SECURITY / REGRESSION (4)
async def test_security_cleanup_failure_does_not_leave_mapping(monkeypatch):
    async def stop(proc): raise OSError('cleanup failed')
    monkeypatch.setattr(run_shell, '_stop_shell_process', stop)
    class P(FakeProcess):
        async def communicate(self): raise OSError('transport')
    async def spawn(*args, **kwargs): return P()
    monkeypatch.setattr(run_shell, '_spawn_shell_process', spawn)
    task = asyncio.create_task(RunShellTool().execute({'command':'safe'}, None))
    result = await task
    assert not result.success
    await asyncio.sleep(0)
    assert RunShellTool.pending_cleanup_for_task(task) is None


async def test_security_normal_success_no_cleanup_mapping():
    result_task = asyncio.create_task(RunShellTool().execute({'command':'printf SAFE'}, None))
    result = await result_task
    assert result.success and result.output['stdout'] == 'SAFE'
    assert RunShellTool.pending_cleanup_for_task(result_task) is None


async def test_security_mapping_does_not_leak_to_other_task(monkeypatch):
    task, release = await _fake_cancelled_tool(monkeypatch)
    try:
        async def other():
            return RunShellTool.pending_cleanup_for_task(asyncio.current_task())
        assert await asyncio.create_task(other()) is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        release.set()
        await asyncio.gather(*run_shell._PENDING_ACTIVE_CLEANUPS, return_exceptions=True)


async def test_security_no_pending_cleanup_for_untracked_task():
    task = asyncio.create_task(asyncio.sleep(0))
    await task
    assert RunShellTool.pending_cleanup_for_task(task) is None

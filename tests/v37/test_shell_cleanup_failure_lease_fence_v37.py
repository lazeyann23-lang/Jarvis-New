"""V37: failed shell cleanup must not release a durable mission execution trace.

All tests are local and non-destructive. Real SQLite tests use the production
SQL via a test-only stdlib async adapter; no Windows end-to-end is claimed.
"""
from __future__ import annotations

import asyncio
import shlex
import sys
from uuid import uuid4

import pytest

from jarvis.plugins.tool import run_shell
from jarvis.plugins.tool.run_shell import RunShellTool
from tests.v27.test_run_shell_cancel_tree_v27 import (
    SafeShell, database, executor_with_store, mission, state_event, wait_for_file,
)

pytestmark = pytest.mark.asyncio
posix = pytest.mark.skipif(sys.platform == 'win32', reason='POSIX-only real subprocess test')


class FailedTransport:
    pid = 123
    returncode = None

    async def communicate(self):
        raise OSError('simulated pipe failure')


async def _fake_shell(monkeypatch, *, fail: bool):
    async def spawn(*a, **kw):
        return FailedTransport()

    async def stop(proc):
        if fail:
            raise OSError('simulated cleanup failure')

    monkeypatch.setattr(run_shell, '_spawn_shell_process', spawn)
    monkeypatch.setattr(run_shell, '_stop_shell_process', stop)


async def _wait_lease(conn, trace, outcome, timeout=2):
    async def check():
        while True:
            row = conn.execute(
                'SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?',
                (str(trace),),
            ).fetchone()
            if row == (outcome,):
                return
            await asyncio.sleep(.01)
    await asyncio.wait_for(check(), timeout)


# FUNCTION (3)
async def test_function_cleanup_failure_survives_task_completion(monkeypatch):
    await _fake_shell(monkeypatch, fail=True)
    task = asyncio.create_task(RunShellTool().execute({'command': 'safe'}, None))
    result = await task
    await asyncio.sleep(0)
    assert not result.success
    assert RunShellTool.pending_cleanup_for_task(task) is None
    assert RunShellTool.cleanup_failed_for_task(task)


async def test_function_successful_cleanup_does_not_set_failure(monkeypatch):
    await _fake_shell(monkeypatch, fail=False)
    task = asyncio.create_task(RunShellTool().execute({'command': 'safe'}, None))
    result = await task
    await asyncio.sleep(0)
    assert not result.success
    assert not RunShellTool.cleanup_failed_for_task(task)


async def test_function_cancelled_cleanup_is_reported(monkeypatch):
    entered = asyncio.Event()
    async def stop(proc):
        entered.set()
        await asyncio.sleep(60)
    await _fake_shell(monkeypatch, fail=False)
    monkeypatch.setattr(run_shell, '_stop_shell_process', stop)
    task = asyncio.create_task(RunShellTool().execute({'command': 'safe'}, None))
    await asyncio.wait_for(entered.wait(), 2)
    cleanup = RunShellTool.pending_cleanup_for_task(task)
    assert cleanup is not None
    cleanup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    await asyncio.sleep(0)
    assert RunShellTool.cleanup_failed_for_task(task)


# INTEGRATION (3)
async def test_integration_failed_cleanup_leaves_sqlite_lease_running(tmp_path, monkeypatch):
    a = database(tmp_path/'missions.db')
    b = database(tmp_path/'missions.db')
    try:
        mission(a)
        executor = executor_with_store(monkeypatch, a)
        await _fake_shell(monkeypatch, fail=True)
        trace = uuid4()
        result = await executor.execute(
            SafeShell(), {'command': 'safe'},
            config_snapshot={'tool_origin': 'mission_worker', 'mission_id': 'm1'},
            trace_id=trace,
        )
        assert not result.success
        await _wait_lease(b, trace, 'running')
        assert b.execute('SELECT activated_ms FROM mission_tool_execution_leases '
                         'WHERE trace_id=?', (str(trace),)).fetchone()[0] is not None
        # The failed cleanup cannot silently make this trace reusable.
        assert b.execute('SELECT count(*) FROM mission_tool_execution_leases '
                         'WHERE trace_id=?', (str(trace),)).fetchone() == (1,)
    finally:
        a.close(); b.close()


@posix
async def test_integration_real_process_cleanup_error_keeps_lease(tmp_path, monkeypatch):
    a = database(tmp_path/'missions.db')
    b = database(tmp_path/'missions.db')
    mission(a)
    executor = executor_with_store(monkeypatch, a)
    marker = tmp_path/'started.txt'
    command = f'printf START > {shlex.quote(str(marker))}; sleep 30'
    original = run_shell._stop_shell_process
    async def stop_then_raise(proc):
        await original(proc)  # no orphan even when this test injects a failure
        raise OSError('post-reap verification failed')
    monkeypatch.setattr(run_shell, '_stop_shell_process', stop_then_raise)
    trace = uuid4()
    task = asyncio.create_task(executor.execute(
        SafeShell(), {'command': command, 'timeout_s': 15},
        config_snapshot={'tool_origin': 'mission_worker', 'mission_id': 'm1'},
        trace_id=trace,
    ))
    try:
        await wait_for_file(marker)
        state_event(b, 'CANCELLED')
        result = await asyncio.wait_for(task, 5)
        assert not result.success
        await _wait_lease(b, trace, 'running')
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await asyncio.gather(*run_shell._PENDING_ACTIVE_CLEANUPS, return_exceptions=True)
        a.close(); b.close()


@posix
async def test_integration_double_cancel_failed_deferred_cleanup_keeps_lease(tmp_path, monkeypatch):
    a = database(tmp_path/'missions.db')
    b = database(tmp_path/'missions.db')
    mission(a)
    executor = executor_with_store(monkeypatch, a)
    marker = tmp_path/'started-double.txt'
    command = f'printf START > {shlex.quote(str(marker))}; sleep 30'
    original = run_shell._stop_shell_process
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed_failure(proc):
        entered.set()
        await release.wait()
        await original(proc)
        raise OSError('injected deferred cleanup failure')
    monkeypatch.setattr(run_shell, '_stop_shell_process', delayed_failure)
    trace = uuid4()
    task = asyncio.create_task(executor.execute(
        SafeShell(), {'command': command, 'timeout_s': 15},
        config_snapshot={'tool_origin': 'mission_worker', 'mission_id': 'm1'},
        trace_id=trace,
    ))
    try:
        await wait_for_file(marker)
        state_event(b, 'CANCELLED')
        await asyncio.wait_for(entered.wait(), 3)
        owners = [t for t in run_shell._TOOL_TASK_CLEANUPS if not t.done()]
        assert len(owners) == 1
        owners[0].cancel()  # second cancellation ends Python task, not cleanup
        result = await asyncio.wait_for(task, 5)
        assert not result.success
        assert b.execute('SELECT outcome FROM mission_tool_execution_leases '
                         'WHERE trace_id=?', (str(trace),)).fetchone() == ('running',)
        release.set()
        await asyncio.gather(*run_shell._PENDING_ACTIVE_CLEANUPS, return_exceptions=True)
        await asyncio.sleep(.1)  # deferred finisher must refuse cleanup failure
        assert b.execute('SELECT outcome FROM mission_tool_execution_leases '
                         'WHERE trace_id=?', (str(trace),)).fetchone() == ('running',)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await asyncio.gather(*run_shell._PENDING_ACTIVE_CLEANUPS, return_exceptions=True)
        a.close(); b.close()


async def test_integration_successful_cleanup_still_finalizes(tmp_path, monkeypatch):
    a = database(tmp_path/'missions.db')
    b = database(tmp_path/'missions.db')
    try:
        mission(a)
        executor = executor_with_store(monkeypatch, a)
        await _fake_shell(monkeypatch, fail=False)
        trace = uuid4()
        result = await executor.execute(
            SafeShell(), {'command': 'safe'},
            config_snapshot={'tool_origin': 'mission_worker', 'mission_id': 'm1'},
            trace_id=trace,
        )
        assert not result.success
        await _wait_lease(b, trace, 'failed')
    finally:
        a.close(); b.close()


# SECURITY / REGRESSION (4)
async def test_security_failed_cleanup_never_replays_trace(tmp_path, monkeypatch):
    a = database(tmp_path/'missions.db')
    b = database(tmp_path/'missions.db')
    try:
        mission(a)
        executor = executor_with_store(monkeypatch, a)
        await _fake_shell(monkeypatch, fail=True)
        trace = uuid4()
        opts = {'tool_origin': 'mission_worker', 'mission_id': 'm1'}
        first = await executor.execute(SafeShell(), {'command': 'safe'},
                                       config_snapshot=opts, trace_id=trace)
        assert not first.success
        second = await executor.execute(SafeShell(), {'command': 'safe'},
                                        config_snapshot=opts, trace_id=trace)
        assert not second.success
        assert 'mission_execution_claim_refused' in second.error
        assert b.execute('SELECT outcome FROM mission_tool_execution_leases '
                         'WHERE trace_id=?', (str(trace),)).fetchone() == ('running',)
    finally:
        a.close(); b.close()


async def test_security_cleanup_failure_is_scoped_to_own_task(monkeypatch):
    await _fake_shell(monkeypatch, fail=True)
    bad = asyncio.create_task(RunShellTool().execute({'command': 'safe'}, None))
    await bad
    other = asyncio.create_task(asyncio.sleep(0))
    await other
    await asyncio.sleep(0)
    assert RunShellTool.cleanup_failed_for_task(bad)
    assert not RunShellTool.cleanup_failed_for_task(other)


async def test_security_cleanup_status_hook_exception_fails_closed(tmp_path, monkeypatch):
    a = database(tmp_path/'missions.db')
    try:
        mission(a)
        executor = executor_with_store(monkeypatch, a)
        await _fake_shell(monkeypatch, fail=False)
        class BrokenStatusShell(SafeShell):
            @staticmethod
            def cleanup_failed_for_task(task):
                raise RuntimeError('status unavailable')
        trace = uuid4()
        result = await executor.execute(
            BrokenStatusShell(), {'command': 'safe'},
            config_snapshot={'tool_origin': 'mission_worker', 'mission_id': 'm1'},
            trace_id=trace,
        )
        assert not result.success
        await _wait_lease(a, trace, 'running')
    finally:
        a.close()


async def test_security_no_cleanup_needed_normal_command(tmp_path):
    task = asyncio.create_task(RunShellTool().execute({'command': 'printf SAFE'}, None))
    result = await task
    assert result.success and result.output['stdout'] == 'SAFE'
    assert not RunShellTool.cleanup_failed_for_task(task)

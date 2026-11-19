"""V38: POSIX process-group and reaping failures must fail closed.

Tests are local, do not require elevated privileges, and never send a real
signal to an unrelated process. The SQLite tests use the production SQL and
V27's test-only stdlib async adapter, not production aiosqlite.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import sys
from uuid import uuid4

import pytest

from jarvis.plugins.tool import run_shell
from jarvis.plugins.tool.run_shell import RunShellTool
from tests.v27.test_run_shell_cancel_tree_v27 import (
    SafeShell, database, executor_with_store, mission, wait_for_file,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(sys.platform == 'win32', reason='POSIX only')]


class FakeProcess:
    pid = 12038
    returncode = None

    def __init__(self, *, wait_timeout=False, kill_error=False):
        self.kills = 0
        self.waits = 0
        self.wait_timeout = wait_timeout
        self.kill_error = kill_error

    def kill(self):
        self.kills += 1
        if self.kill_error:
            raise PermissionError('direct child kill denied')

    async def wait(self):
        self.waits += 1
        if self.wait_timeout:
            raise TimeoutError('simulated process wait timeout')
        self.returncode = -9
        return self.returncode

    async def communicate(self):
        raise OSError('simulated pipe failure')


def fail_group_kill(monkeypatch):
    def denied(pid, sig):
        raise PermissionError('simulated process group permission error')
    monkeypatch.setattr(run_shell.os, 'killpg', denied)


def no_group(monkeypatch):
    def missing(pid, sig):
        raise ProcessLookupError('group already exited')
    monkeypatch.setattr(run_shell.os, 'killpg', missing)


async def fake_spawn(monkeypatch, proc):
    async def spawn(*args, **kwargs):
        return proc
    monkeypatch.setattr(run_shell, '_spawn_shell_process', spawn)


# FUNCTION: real cleanup code, deterministic fake process handles (4)
async def test_function_group_kill_error_reaps_direct_child_then_raises(monkeypatch):
    proc = FakeProcess()
    fail_group_kill(monkeypatch)
    with pytest.raises(RuntimeError, match='process-tree cleanup failed') as err:
        await run_shell._stop_shell_process(proc)
    assert isinstance(err.value.__cause__, PermissionError)
    assert (proc.kills, proc.waits) == (1, 1)


async def test_function_reaping_timeout_is_a_failure(monkeypatch):
    proc = FakeProcess(wait_timeout=True)
    no_group(monkeypatch)
    with pytest.raises(RuntimeError, match='did not reap'):
        await run_shell._stop_shell_process(proc)
    assert (proc.kills, proc.waits) == (1, 1)


async def test_function_missing_group_is_benign_if_child_reaps(monkeypatch):
    proc = FakeProcess()
    no_group(monkeypatch)
    await run_shell._stop_shell_process(proc)
    assert (proc.kills, proc.waits) == (1, 1)


async def test_function_cancelled_spawn_reaper_preserves_group_failure(monkeypatch):
    proc = FakeProcess()
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        return proc

    monkeypatch.setattr(run_shell.asyncio, 'create_subprocess_exec', delayed_spawn)
    fail_group_kill(monkeypatch)
    owner = asyncio.create_task(run_shell._spawn_shell_process('safe'))
    await asyncio.wait_for(entered.wait(), 2)
    owner.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(owner, 2)
    await asyncio.sleep(0)
    assert (proc.kills, proc.waits) == (1, 1)
    assert run_shell.cleanup_failed_for_task(owner)
    assert not run_shell._PENDING_SPAWN_CLEANUPS


# INTEGRATION: real subprocess and/or production ToolExecutor + SQLite (4)
async def test_integration_real_posix_group_cleanup_success(tmp_path):
    marker = tmp_path / 'started'
    command = f'printf started > {shlex.quote(str(marker))}; exec sleep 30'
    task = asyncio.create_task(RunShellTool().execute({'command': command, 'timeout_s': 15}, None))
    try:
        await wait_for_file(marker)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert not RunShellTool.cleanup_failed_for_task(task)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_integration_real_posix_group_signal_failure_keeps_sqlite_lease(tmp_path, monkeypatch):
    a = database(tmp_path / 'missions.db')
    b = database(tmp_path / 'missions.db')
    mission(a)
    executor = executor_with_store(monkeypatch, a)
    marker = tmp_path / 'started'
    command = f'printf started > {shlex.quote(str(marker))}; exec sleep 30'
    original_killpg = run_shell.os.killpg

    def denied_group(pid, sig):
        raise PermissionError('simulated group signal failure')

    monkeypatch.setattr(run_shell.os, 'killpg', denied_group)
    trace = uuid4()
    task = asyncio.create_task(executor.execute(
        SafeShell(), {'command': command, 'timeout_s': 15},
        config_snapshot={'tool_origin': 'mission_worker', 'mission_id': 'm1'},
        trace_id=trace,
    ))
    try:
        await wait_for_file(marker)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert b.execute('SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?',
                         (str(trace),)).fetchone() == ('running',)
    finally:
        monkeypatch.setattr(run_shell.os, 'killpg', original_killpg)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await asyncio.gather(*run_shell._PENDING_ACTIVE_CLEANUPS, return_exceptions=True)
        a.close(); b.close()


async def test_integration_tool_executor_group_failure_survives_reconnect(tmp_path, monkeypatch):
    db = tmp_path / 'missions.db'
    a = database(db)
    mission(a)
    executor = executor_with_store(monkeypatch, a)
    proc = FakeProcess()
    await fake_spawn(monkeypatch, proc)
    fail_group_kill(monkeypatch)
    trace = uuid4()
    result = await executor.execute(
        SafeShell(), {'command': 'safe'},
        config_snapshot={'tool_origin': 'mission_worker', 'mission_id': 'm1'},
        trace_id=trace,
    )
    assert not result.success
    a.close()
    b = database(db)
    try:
        assert b.execute('SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?',
                         (str(trace),)).fetchone() == ('running',)
    finally:
        b.close()


async def test_integration_cancel_during_spawn_failed_reaper_keeps_sqlite_lease(tmp_path, monkeypatch):
    a = database(tmp_path / 'missions.db')
    b = database(tmp_path / 'missions.db')
    mission(a)
    executor = executor_with_store(monkeypatch, a)
    proc = FakeProcess()
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        return proc

    monkeypatch.setattr(run_shell.asyncio, 'create_subprocess_exec', delayed_spawn)
    fail_group_kill(monkeypatch)
    trace = uuid4()
    task = asyncio.create_task(executor.execute(
        SafeShell(), {'command': 'safe'},
        config_snapshot={'tool_origin': 'mission_worker', 'mission_id': 'm1'},
        trace_id=trace,
    ))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert (proc.kills, proc.waits) == (1, 1)
        assert b.execute('SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?',
                         (str(trace),)).fetchone() == ('running',)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await asyncio.gather(*run_shell._PENDING_SPAWN_CLEANUPS, return_exceptions=True)
        a.close(); b.close()


# SECURITY / REGRESSION: error markers, replay fence and independent traces (4)
async def test_security_direct_kill_error_also_fails_closed_after_wait(monkeypatch):
    proc = FakeProcess(kill_error=True)
    no_group(monkeypatch)
    with pytest.raises(RuntimeError, match='process-tree cleanup failed'):
        await run_shell._stop_shell_process(proc)
    assert (proc.kills, proc.waits) == (1, 1)


async def test_security_error_marker_persists_after_cleanup_task_ends(monkeypatch):
    proc = FakeProcess()
    await fake_spawn(monkeypatch, proc)
    fail_group_kill(monkeypatch)
    task = asyncio.create_task(RunShellTool().execute({'command': 'safe'}, None))
    result = await task
    await asyncio.sleep(0)
    assert not result.success
    assert RunShellTool.pending_cleanup_for_task(task) is None
    assert RunShellTool.cleanup_failed_for_task(task)


async def test_security_failed_group_kill_blocks_trace_replay(tmp_path, monkeypatch):
    a = database(tmp_path / 'missions.db')
    b = database(tmp_path / 'missions.db')
    try:
        mission(a)
        executor = executor_with_store(monkeypatch, a)
        proc = FakeProcess()
        await fake_spawn(monkeypatch, proc)
        fail_group_kill(monkeypatch)
        trace = uuid4()
        opts = {'tool_origin': 'mission_worker', 'mission_id': 'm1'}
        first = await executor.execute(SafeShell(), {'command': 'safe'},
                                       config_snapshot=opts, trace_id=trace)
        second = await executor.execute(SafeShell(), {'command': 'safe'},
                                        config_snapshot=opts, trace_id=trace)
        assert not first.success and not second.success
        assert 'mission_execution_claim_refused' in second.error
        assert b.execute('SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?',
                         (str(trace),)).fetchone() == ('running',)
        assert proc.kills == 1  # replay did not start another OS command
    finally:
        a.close(); b.close()


async def test_security_successful_independent_trace_still_finalizes(tmp_path, monkeypatch):
    a = database(tmp_path / 'missions.db')
    b = database(tmp_path / 'missions.db')
    try:
        mission(a)
        executor = executor_with_store(monkeypatch, a)
        proc = FakeProcess()
        await fake_spawn(monkeypatch, proc)
        fail_group_kill(monkeypatch)
        opts = {'tool_origin': 'mission_worker', 'mission_id': 'm1'}
        bad = uuid4()
        await executor.execute(SafeShell(), {'command': 'safe'}, config_snapshot=opts, trace_id=bad)
        no_group(monkeypatch)
        good = uuid4()
        await executor.execute(SafeShell(), {'command': 'safe'}, config_snapshot=opts, trace_id=good)
        assert b.execute('SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?',
                         (str(bad),)).fetchone() == ('running',)
        assert b.execute('SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?',
                         (str(good),)).fetchone() == ('failed',)
    finally:
        a.close(); b.close()

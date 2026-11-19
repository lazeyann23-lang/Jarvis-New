"""V32: cancelled subprocess creation must never orphan a shell process.

Function, integration, and security tests. POSIX integration uses harmless
heartbeat children; Windows-specific subprocess behaviour remains untested.
"""
from __future__ import annotations

import asyncio
import shlex
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from jarvis.plugins.tool import run_shell
from jarvis.plugins.tool.run_shell import RunShellTool
from tests.v27.test_run_shell_cancel_tree_v27 import (
    SafeShell, assert_stopped, database, executor_with_store, heartbeat_command,
    mission, wait_for_file,
)

pytestmark = pytest.mark.asyncio
posix = pytest.mark.skipif(sys.platform == 'win32', reason='POSIX process group')


class FakeProcess:
    pid = 99
    returncode = None
    def __init__(self):
        self.killed = False
        self.reaped = False
    def kill(self): self.killed = True
    async def wait(self): self.reaped = True


# FUNCTION: direct spawn lifecycle and failure handling.
async def test_function_successful_spawn_returns_process(monkeypatch):
    proc = FakeProcess()
    async def fake_spawn(*a, **kw): return proc
    monkeypatch.setattr(run_shell.asyncio, 'create_subprocess_exec', fake_spawn)
    assert await run_shell._spawn_shell_process('safe') is proc
    assert not proc.killed and not run_shell._PENDING_SPAWN_CLEANUPS


async def test_function_cancel_during_spawn_reaps_process(monkeypatch):
    gate = asyncio.Event(); started = asyncio.Event(); proc = FakeProcess(); stopped = []
    async def fake_spawn(*a, **kw):
        started.set(); await gate.wait(); return proc
    async def fake_stop(p):
        stopped.append(p); p.kill(); await p.wait()
    monkeypatch.setattr(run_shell.asyncio, 'create_subprocess_exec', fake_spawn)
    monkeypatch.setattr(run_shell, '_stop_shell_process', fake_stop)
    task = asyncio.create_task(run_shell._spawn_shell_process('safe'))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel(); gate.set()
    with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(task, 2)
    await asyncio.sleep(0)
    assert stopped == [proc] and proc.killed and proc.reaped
    assert not run_shell._PENDING_SPAWN_CLEANUPS


async def test_function_spawn_error_is_preserved(monkeypatch):
    async def fake_spawn(*a, **kw): raise FileNotFoundError('missing')
    monkeypatch.setattr(run_shell.asyncio, 'create_subprocess_exec', fake_spawn)
    with pytest.raises(FileNotFoundError): await run_shell._spawn_shell_process('missing')


# INTEGRATION: real child created but spawn await has not returned a handle.
@posix
async def test_integration_cancel_after_os_spawn_kills_real_child(tmp_path, monkeypatch):
    original = asyncio.create_subprocess_exec
    spawned = asyncio.Event(); release = asyncio.Event()
    async def delayed(*a, **kw):
        proc = await original(*a, **kw)
        spawned.set()
        await release.wait()
        return proc
    monkeypatch.setattr(run_shell.asyncio, 'create_subprocess_exec', delayed)
    command, marker = heartbeat_command(tmp_path)
    task = asyncio.create_task(RunShellTool().execute({'command': command, 'timeout_s': 10}, None))
    try:
        await asyncio.wait_for(spawned.wait(), 3)
        await wait_for_file(marker)
        task.cancel(); release.set()
        with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(task, 5)
        await assert_stopped(marker)
        assert not run_shell._PENDING_SPAWN_CLEANUPS
    finally:
        release.set()
        if not task.done():
            task.cancel(); await asyncio.gather(task, return_exceptions=True)


@posix
async def test_integration_persisted_cancel_while_spawn_pending_reaps_child(tmp_path, monkeypatch):
    a = database(tmp_path/'m.db'); b = database(tmp_path/'m.db')
    mission(a)
    executor = executor_with_store(monkeypatch, a)
    original = asyncio.create_subprocess_exec
    spawned = asyncio.Event(); release = asyncio.Event()
    async def delayed(*args, **kwargs):
        proc = await original(*args, **kwargs)
        spawned.set()
        await release.wait()
        return proc
    monkeypatch.setattr(run_shell.asyncio, 'create_subprocess_exec', delayed)
    command, marker = heartbeat_command(tmp_path)
    trace = uuid4()
    task = asyncio.create_task(executor.execute(
        SafeShell(), {'command': command, 'timeout_s': 10},
        config_snapshot={'tool_origin': 'mission_worker', 'mission_id': 'm1'},
        trace_id=trace,
    ))
    try:
        await asyncio.wait_for(spawned.wait(), 3)
        await wait_for_file(marker)
        b.execute("UPDATE missions SET state='CANCELLED' WHERE id='m1'")
        await asyncio.sleep(.35)  # allow lifecycle watchdog to cancel the tool
        release.set()
        result = await asyncio.wait_for(task, 5)
        assert not result.success and 'mission_cancelled_during_execution' in result.error
        assert a.execute('SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?',
                         (str(trace),)).fetchone() == ('cancelled',)
        await assert_stopped(marker)
    finally:
        release.set()
        if not task.done(): task.cancel(); await asyncio.gather(task, return_exceptions=True)
        a.close(); b.close()


# SECURITY / REGRESSION: double cancellation, no child, no changed legacy path.
async def test_security_second_cancel_does_not_abandon_reaper(monkeypatch):
    spawn_gate = asyncio.Event(); stop_gate = asyncio.Event()
    started = asyncio.Event(); stopping = asyncio.Event(); proc = FakeProcess()
    async def fake_spawn(*a, **kw):
        started.set(); await spawn_gate.wait(); return proc
    async def fake_stop(p):
        stopping.set(); await stop_gate.wait(); p.kill(); await p.wait()
    monkeypatch.setattr(run_shell.asyncio, 'create_subprocess_exec', fake_spawn)
    monkeypatch.setattr(run_shell, '_stop_shell_process', fake_stop)
    task = asyncio.create_task(run_shell._spawn_shell_process('safe'))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel(); spawn_gate.set()
    await asyncio.wait_for(stopping.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(task, 2)
    assert run_shell._PENDING_SPAWN_CLEANUPS
    stop_gate.set()
    for _ in range(20):
        if not run_shell._PENDING_SPAWN_CLEANUPS: break
        await asyncio.sleep(.02)
    assert proc.killed and proc.reaped and not run_shell._PENDING_SPAWN_CLEANUPS


async def test_security_cancelled_spawn_failure_does_not_mask_cancellation(monkeypatch):
    started = asyncio.Event(); release = asyncio.Event()
    async def fake_spawn(*a, **kw):
        started.set(); await release.wait(); raise OSError('spawn denied')
    monkeypatch.setattr(run_shell.asyncio, 'create_subprocess_exec', fake_spawn)
    task = asyncio.create_task(run_shell._spawn_shell_process('safe'))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel(); release.set()
    with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(task, 2)
    await asyncio.sleep(0)
    assert not run_shell._PENDING_SPAWN_CLEANUPS


@posix
async def test_security_cancel_before_os_spawn_does_not_leave_live_process(tmp_path, monkeypatch):
    original = asyncio.create_subprocess_exec
    started = asyncio.Event(); release = asyncio.Event()
    async def delayed(*a, **kw):
        started.set(); await release.wait()
        return await original(*a, **kw)
    monkeypatch.setattr(run_shell.asyncio, 'create_subprocess_exec', delayed)
    cmd, marker = heartbeat_command(tmp_path)
    task = asyncio.create_task(RunShellTool().execute({'command': cmd}, None))
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel(); release.set()
        with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(task, 3)
        if marker.exists():
            await assert_stopped(marker)  # effects before kill are possible; no live child
        assert not run_shell._PENDING_SPAWN_CLEANUPS
    finally:
        release.set()
        if not task.done(): task.cancel()


@posix
async def test_security_normal_shell_output_unchanged():
    result = await RunShellTool().execute({'command': 'printf ok | tr a-z A-Z'}, None)
    assert result.success and result.output['stdout'] == 'OK'

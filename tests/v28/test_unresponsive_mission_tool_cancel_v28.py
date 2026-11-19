"""V28: bounded cancellation of non-cooperative mission tool coroutines.

These tests use the real ToolExecutor and EventBus, with real SQLite (two
independent connections for the cross-connection case). No external tools or
secrets are executed. Non-cooperative tools are deliberately released by each
fixture so the test suite does not leak tasks.
"""
from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from jarvis.core.protocols import ToolResult
from tests.v25.test_mission_execution_leases_v25 import (
    add_event, add_mission, config, database, setup, store,
)

pytestmark = pytest.mark.asyncio


class IgnoresCancellation:
    name = "noncooperative"
    risk_tier = "safe"
    schema = {"type": "object", "properties": {}}

    def __init__(self, *, late_error=False):
        self.started = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.late_error = late_error

    async def execute(self, args, ctx):
        self.calls += 1
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancel_seen.set()
            await self.release.wait()  # deliberately ignores the first cancel
            if self.late_error:
                raise ValueError("late tool error")
            return ToolResult(success=True, output={"side_effect_possible": True})


async def wait_for_lease(conn, trace, outcome):
    async def poll():
        while True:
            row = conn.execute(
                "SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?",
                (str(trace),),
            ).fetchone()
            if row == (outcome,):
                return
            await asyncio.sleep(0.01)
    await asyncio.wait_for(poll(), timeout=2)


# Function (2)
async def test_function_uncooperative_watcher_cancel_returns_bounded(tmp_path, monkeypatch):
    a = database(tmp_path / "missions.db")
    add_mission(a)
    executor, denied = setup(monkeypatch, store(a))
    tool = IgnoresCancellation()
    trace = uuid4()
    try:
        running = asyncio.create_task(executor.execute(
            tool, {}, config_snapshot=config(), trace_id=trace,
        ))
        await asyncio.wait_for(tool.started.wait(), 2)
        add_event(a, "CANCELLED")
        result = await asyncio.wait_for(running, timeout=2.5)
        assert not result.success
        assert "mission_cancelled_during_execution" in result.error
        assert tool.cancel_seen.is_set()
        assert len(executor._unresponsive_mission_tools) == 1
        assert denied[-1].reason == "mission_cancelled_during_execution"
        # The tool is *still running*: never claim it was stopped in the DB.
        assert a.execute(
            "SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?",
            (str(trace),),
        ).fetchone() == ("running",)
    finally:
        tool.release.set()
        await wait_for_lease(a, trace, "cancelled")
        assert not executor._unresponsive_mission_tools
        a.close()


async def test_function_cooperative_tool_still_finishes_normally(tmp_path, monkeypatch):
    from tests.v25.test_mission_execution_leases_v25 import FakeTool

    a = database(tmp_path / "missions.db")
    add_mission(a)
    executor, _ = setup(monkeypatch, store(a))
    tool = FakeTool()
    tool.block = True
    trace = uuid4()
    running = asyncio.create_task(executor.execute(
        tool, {}, config_snapshot=config(), trace_id=trace,
    ))
    await asyncio.wait_for(tool.started.wait(), 2)
    add_event(a, "CANCELLED")
    result = await asyncio.wait_for(running, 2)
    assert not result.success and tool.cancelled
    assert not executor._unresponsive_mission_tools
    await wait_for_lease(a, trace, "cancelled")
    a.close()


# Integration (2)
async def test_integration_other_connection_cancel_noncooperative_and_no_replay(
    tmp_path, monkeypatch,
):
    path = tmp_path / "missions.db"
    a, b = database(path), database(path)
    add_mission(a)
    executor, _ = setup(monkeypatch, store(a))
    tool = IgnoresCancellation()
    trace = uuid4()
    try:
        running = asyncio.create_task(executor.execute(
            tool, {}, config_snapshot=config(), trace_id=trace,
        ))
        await asyncio.wait_for(tool.started.wait(), 2)
        add_event(b, "CANCELLED")
        result = await asyncio.wait_for(running, 2.5)
        assert not result.success
        assert b.execute(
            "SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?",
            (str(trace),),
        ).fetchone() == ("running",)
        # A second process/connection cannot reuse the trace while it lives.
        assert not await store(b).try_start_tool_execution(
            trace_id=str(trace), mission_id="m1", tool_name=tool.name,
        )
        assert tool.calls == 1
    finally:
        tool.release.set()
        await wait_for_lease(b, trace, "cancelled")
        a.close(); b.close()


async def test_integration_late_exception_finalizes_auditable_lease(tmp_path, monkeypatch):
    a = database(tmp_path / "missions.db")
    add_mission(a)
    executor, _ = setup(monkeypatch, store(a))
    tool = IgnoresCancellation(late_error=True)
    trace = uuid4()
    try:
        running = asyncio.create_task(executor.execute(
            tool, {}, config_snapshot=config(), trace_id=trace,
        ))
        await asyncio.wait_for(tool.started.wait(), 2)
        add_event(a, "CANCELLED")
        result = await asyncio.wait_for(running, 2.5)
        assert not result.success
        assert a.execute(
            "SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?",
            (str(trace),),
        ).fetchone() == ("running",)
    finally:
        tool.release.set()
        await wait_for_lease(a, trace, "cancelled")
        a.close()


# Security / regression (2)
async def test_security_caller_cancel_does_not_hang_and_keeps_lease(tmp_path, monkeypatch):
    a = database(tmp_path / "missions.db")
    add_mission(a)
    executor, _ = setup(monkeypatch, store(a))
    tool = IgnoresCancellation()
    trace = uuid4()
    try:
        running = asyncio.create_task(executor.execute(
            tool, {}, config_snapshot=config(), trace_id=trace,
        ))
        await asyncio.wait_for(tool.started.wait(), 2)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, 2)
        assert tool.cancel_seen.is_set()
        assert a.execute(
            "SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?",
            (str(trace),),
        ).fetchone() == ("running",)
    finally:
        tool.release.set()
        await wait_for_lease(a, trace, "cancelled")
        a.close()


async def test_security_replay_denied_even_if_orphan_tool_later_succeeds(
    tmp_path, monkeypatch,
):
    a = database(tmp_path / "missions.db")
    add_mission(a)
    executor, denied = setup(monkeypatch, store(a))
    tool = IgnoresCancellation()
    trace = uuid4()
    try:
        running = asyncio.create_task(executor.execute(
            tool, {}, config_snapshot=config(), trace_id=trace,
        ))
        await asyncio.wait_for(tool.started.wait(), 2)
        add_event(a, "CANCELLED")
        await asyncio.wait_for(running, 2.5)
        tool.release.set()
        await wait_for_lease(a, trace, "cancelled")
        # Even a late successful coroutine result never reopens a trace.
        replay = await executor.execute(
            tool, {}, config_snapshot=config(), trace_id=trace,
        )
        assert not replay.success and tool.calls == 1
        assert denied[-1].reason in {
            "mission_execution_claim_refused", "mission_inactive",
            "mission_inactive_for_execution", "mission_inactive_before_execution",
        }
    finally:
        tool.release.set()
        a.close()

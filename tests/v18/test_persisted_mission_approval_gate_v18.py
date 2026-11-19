"""V18: persisted mission state gates manual and automatic tool approvals.

The SQLite table is a minimal durable state fixture, NOT MissionManager's
production database. Two independent bus/coordinator stacks simulate restart
and a second process; no external tools are invoked.
"""
import asyncio
import sqlite3
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

# The test environment lacks aiosqlite. Shim only imports; no DB operations
# in this suite rely on it. The SQLite fixture uses stdlib sqlite3.
try:
    import aiosqlite  # noqa: F401
except ImportError:
    shim = types.ModuleType("aiosqlite")
    for name in ("Connection", "Cursor", "Row"):
        setattr(shim, name, type(name, (), {}))
    sys.modules["aiosqlite"] = shim

from jarvis.core.bus import EventBus
from jarvis.core.events import ActionApprovalRequired, ActionApproved, ActionDenied
from jarvis.missions.state_machine import MissionState
from jarvis.missions.tool_approvals import MissionToolApprovalCoordinator, MissionToolAutoApprover
from jarvis.ui.web.server import WebServer


def persist(path: Path, mission: str, state: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS states (mission TEXT PRIMARY KEY, state TEXT NOT NULL)")
        conn.execute("INSERT OR REPLACE INTO states VALUES (?, ?)", (mission, state))


def persisted_reader(path: Path):
    async def read(mission: str) -> bool:
        def query():
            with sqlite3.connect(path) as conn:
                row = conn.execute("SELECT state FROM states WHERE mission=?", (mission,)).fetchone()
                return bool(row and row[0] in {"RUNNING", "CRITIQUING", "LOOPING"})
        return await asyncio.to_thread(query)
    return read


def stack(reader):
    bus = EventBus()
    coordinator = MissionToolApprovalCoordinator(bus, mission_is_active=reader)
    auto = MissionToolAutoApprover(bus, coordinator=coordinator)
    approvals, denials = [], []
    async def on_approved(event):
        approvals.append(event)
    async def on_denied(event):
        denials.append(event)
    bus.subscribe(ActionApproved, on_approved)
    bus.subscribe(ActionDenied, on_denied)
    return bus, coordinator, auto, approvals, denials


def request(mission="m1", tool="external-action", tier="ask"):
    return ActionApprovalRequired(
        trace_id=uuid4(), mission_id=mission, tool_name=tool,
        risk_tier=tier, reason="risk_tier", args_preview="{}",
        expires_at_ns=time.time_ns() + 30_000_000_000,
    )


# FUNCTION
@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["RUNNING", "CRITIQUING", "LOOPING"])
async def test_active_persisted_states_allow_granted_tools(tmp_path, state):
    db = tmp_path / "states.db"
    persist(db, "m1", state)
    bus, coord, auto, approved, denied = stack(persisted_reader(db))
    auto.arm("m1", "grant", frozenset({"external-action"}))
    event = request()
    await bus.publish(event)
    assert [x.trace_id for x in approved] == [event.trace_id]
    assert denied == []
    assert await coord.list_pending("m1") == ()


@pytest.mark.asyncio
async def test_missing_mission_refuses_approval(tmp_path):
    db = tmp_path / "states.db"
    persist(db, "another", "RUNNING")
    bus, coord, auto, approved, denied = stack(persisted_reader(db))
    auto.arm("m1", "grant", frozenset({"external-action"}))
    event = request()
    await bus.publish(event)
    assert approved == []
    assert [x.trace_id for x in denied] == [event.trace_id]
    assert await coord.list_pending("m1") == ()


# INTEGRATION
@pytest.mark.asyncio
async def test_separate_instance_cancel_is_visible_to_existing_auto_approver(tmp_path):
    db = tmp_path / "states.db"
    persist(db, "m1", "RUNNING")
    bus, coord, auto, approved, denied = stack(persisted_reader(db))
    auto.arm("m1", "grant", frozenset({"external-action"}))
    await bus.publish(request())
    assert len(approved) == 1
    # Simulates an independent server instance persisting a terminal state.
    await asyncio.to_thread(persist, db, "m1", "CANCELLED")
    event = request()
    await bus.publish(event)
    assert len(approved) == 1
    assert denied[-1].trace_id == event.trace_id
    assert await coord.list_pending("m1") == ()


@pytest.mark.asyncio
async def test_restart_does_not_erase_cancelled_state_barrier(tmp_path):
    db = tmp_path / "states.db"
    persist(db, "m1", "CANCELLED")
    for _ in range(2):
        bus, coord, auto, approved, denied = stack(persisted_reader(db))
        auto.arm("m1", "new-process-grant", frozenset({"external-action"}))
        event = request()
        await bus.publish(event)
        assert approved == []
        assert [x.trace_id for x in denied] == [event.trace_id]
        assert await coord.list_pending("m1") == ()


# SECURITY / REGRESSION
@pytest.mark.asyncio
async def test_database_error_fails_closed_without_auto_approval():
    async def broken(_mission):
        raise sqlite3.OperationalError("DB unavailable")
    bus, coord, auto, approved, denied = stack(broken)
    auto.arm("m1", "grant", frozenset({"external-action"}))
    event = request()
    await bus.publish(event)
    assert approved == []
    assert len(denied) == 1 and denied[0].trace_id == event.trace_id
    assert await coord.list_pending("m1") == ()


@pytest.mark.asyncio
async def test_cancelled_mission_does_not_block_another(tmp_path):
    db = tmp_path / "states.db"
    persist(db, "m1", "CANCELLED")
    persist(db, "m2", "RUNNING")
    bus, _, auto, approved, denied = stack(persisted_reader(db))
    auto.arm("m1", "grant", frozenset({"external-action"}))
    auto.arm("m2", "grant", frozenset({"external-action"}))
    await bus.publish(request("m1"))
    event = request("m2")
    await bus.publish(event)
    assert [x.trace_id for x in approved] == [event.trace_id]
    assert len(denied) == 1


@pytest.mark.asyncio
async def test_persisted_state_does_not_override_block_tier(tmp_path):
    db = tmp_path / "states.db"
    persist(db, "m1", "RUNNING")
    bus, _, auto, approved, _ = stack(persisted_reader(db))
    auto.arm("m1", "grant", frozenset({"external-action"}))
    await bus.publish(request(tier="block"))
    assert approved == []


@pytest.mark.asyncio
async def test_production_server_state_reader_fails_closed():
    fake = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(mission_manager=None)))
    check = WebServer._mission_is_active_for_tool_approval
    assert not await check(fake, "m1")
    class Manager:
        state = MissionState.RUNNING
        async def mission(self, mission):
            return SimpleNamespace(state=self.state)
    manager = Manager()
    fake.app.state.mission_manager = manager
    assert await check(fake, "m1")
    manager.state = MissionState.WAITING_CAPACITY
    assert not await check(fake, "m1")
    manager.state = MissionState.CANCELLED
    assert not await check(fake, "m1")

"""V15 concurrent REST cancel/approve/deny tests with in-memory ASGI stack."""
import asyncio
import sys
import time
import types
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

if "aiosqlite" not in sys.modules:
    shim = types.ModuleType("aiosqlite")
    for name in ("Connection", "Cursor", "Row"):
        setattr(shim, name, type(name, (), {}))
    sys.modules["aiosqlite"] = shim

from jarvis.core.bus import EventBus
from jarvis.core.events import ActionApprovalRequired, ActionApproved
from jarvis.missions.state_machine import MissionState
from jarvis.missions.tool_approvals import MissionToolApprovalCoordinator
from jarvis.ui.web.missions_routes import router


class Store:
    def __init__(self):
        self.events = []

    async def append_and_publish(self, event):
        self.events.append(event)


class Manager:
    def __init__(self):
        self.states = {"mission-1": MissionState.RUNNING, "mission-2": MissionState.RUNNING}
        self.store = Store()
        self.transition_entered = asyncio.Event()
        self.allow_transition = asyncio.Event()
        self.block_transition = False

    async def mission(self, mission_id):
        state = self.states.get(mission_id)
        return SimpleNamespace(mission_id=mission_id, state=state) if state else None

    async def transition_state(self, mission_id, state, **kwargs):
        self.transition_entered.set()
        if self.block_transition:
            await self.allow_transition.wait()
        self.states[mission_id] = state
        return SimpleNamespace(seq=2)


def setup():
    bus = EventBus()
    coordinator = MissionToolApprovalCoordinator(bus)
    manager = Manager()
    app = FastAPI()
    app.include_router(router)
    app.state.mission_manager = manager
    app.state.mission_tool_approvals = coordinator
    return app, bus, coordinator, manager


async def pending(bus, mission_id="mission-1"):
    event = ActionApprovalRequired(
        trace_id=uuid4(), mission_id=mission_id, tool_name="external-action",
        risk_tier="ask", reason="risk_tier", args_preview="{}",
        expires_at_ns=time.time_ns() + 60_000_000_000,
    )
    await bus.publish(event)
    return event


@pytest.mark.asyncio
async def test_cancel_transition_blocks_concurrent_approval_until_revoke():
    app, bus, coord, mgr = setup()
    ticket = await pending(bus)
    approvals = []
    async def record(event):
        approvals.append(event)
    bus.subscribe(ActionApproved, record)
    mgr.block_transition = True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        cancel = asyncio.create_task(client.post("/api/missions/mission-1/cancel"))
        await asyncio.wait_for(mgr.transition_entered.wait(), 2)
        approve = asyncio.create_task(client.post(f"/api/missions/mission-1/tool-approvals/{ticket.trace_id}/approve"))
        await asyncio.sleep(0.02)
        assert not approve.done(), "approval must wait for transition AND revocation"
        mgr.allow_transition.set()
        cancel_result, approve_result = await asyncio.wait_for(asyncio.gather(cancel, approve), 3)
    assert cancel_result.status_code == 200
    assert approve_result.status_code == 409
    assert approvals == []
    assert await coord.list_pending("mission-1") == ()


@pytest.mark.asyncio
async def test_cancel_transition_blocks_concurrent_denial_until_revoke():
    app, bus, _, mgr = setup()
    ticket = await pending(bus)
    mgr.block_transition = True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        cancel = asyncio.create_task(client.post("/api/missions/mission-1/cancel"))
        await asyncio.wait_for(mgr.transition_entered.wait(), 2)
        deny = asyncio.create_task(client.post(f"/api/missions/mission-1/tool-approvals/{ticket.trace_id}/deny", json={"reason":"user_denied"}))
        await asyncio.sleep(0.02)
        assert not deny.done()
        mgr.allow_transition.set()
        a, b = await asyncio.wait_for(asyncio.gather(cancel, deny), 3)
    assert a.status_code == 200
    assert b.status_code == 409


@pytest.mark.asyncio
async def test_other_mission_approval_does_not_wait_for_cancel():
    app, bus, _, mgr = setup()
    await pending(bus, "mission-1")
    other = await pending(bus, "mission-2")
    mgr.block_transition = True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        cancel = asyncio.create_task(client.post("/api/missions/mission-1/cancel"))
        await asyncio.wait_for(mgr.transition_entered.wait(), 2)
        response = await asyncio.wait_for(client.post(f"/api/missions/mission-2/tool-approvals/{other.trace_id}/approve"), 1)
        mgr.allow_transition.set()
        await asyncio.wait_for(cancel, 3)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_approval_that_publishes_first_finishes_before_cancel():
    app, bus, _, mgr = setup()
    ticket = await pending(bus)
    publish_entered = asyncio.Event()
    publish_release = asyncio.Event()
    async def slow_approval(event):
        publish_entered.set()
        await publish_release.wait()
    bus.subscribe(ActionApproved, slow_approval)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        approve = asyncio.create_task(client.post(f"/api/missions/mission-1/tool-approvals/{ticket.trace_id}/approve"))
        await asyncio.wait_for(publish_entered.wait(), 2)
        cancel = asyncio.create_task(client.post("/api/missions/mission-1/cancel"))
        await asyncio.sleep(0.02)
        assert not mgr.transition_entered.is_set()
        publish_release.set()
        approved, cancelled = await asyncio.wait_for(asyncio.gather(approve, cancel), 3)
    assert approved.status_code == 200
    assert cancelled.status_code == 200
    # Already-approved work still needs worker cancellation; not undone here.


@pytest.mark.asyncio
async def test_lock_objects_do_not_accumulate_after_requests():
    _, _, coordinator, _ = setup()
    for i in range(100):
        async with coordinator.mission_decision_guard(f"completed-{i}"):
            pass
    assert len(coordinator._mission_decision_locks) <= 1

@pytest.mark.asyncio
async def test_cancel_without_optional_coordinator_preserves_old_behavior():
    app, _, _, mgr = setup()
    del app.state.mission_tool_approvals
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/missions/mission-1/cancel")
    assert response.status_code == 200
    assert mgr.states["mission-1"] == MissionState.CANCELLED


@pytest.mark.asyncio
async def test_duplicate_approval_emits_only_one_event():
    app, bus, _, _ = setup()
    ticket = await pending(bus)
    approvals = []
    async def record(event):
        approvals.append(event)
    bus.subscribe(ActionApproved, record)
    url = f"/api/missions/mission-1/tool-approvals/{ticket.trace_id}/approve"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first, second = await asyncio.gather(client.post(url), client.post(url))
    assert sorted([first.status_code, second.status_code]) == [200, 404]
    assert len(approvals) == 1

"""V14 mission approval lifecycle: no execution after cancellation/terminal state.

The test-only aiosqlite import shim is necessary because this runner lacks
that dependency. No database methods are exercised by these in-memory tests.
"""
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
from jarvis.core.events import ActionApprovalRequired, ActionApproved, ActionDenied
from jarvis.missions.state_machine import MissionState
from jarvis.safety.approval import ApprovalWorkflow
from jarvis.missions.tool_approvals import MissionToolApprovalCoordinator
from jarvis.ui.web.missions_routes import router


class FakeStore:
    def __init__(self):
        self.events = []

    async def append_and_publish(self, event):
        self.events.append(event)


class FakeManager:
    def __init__(self, state=MissionState.RUNNING):
        self.state = state
        self.store = FakeStore()

    async def mission(self, mission_id):
        if mission_id != "mission-1":
            return None
        return SimpleNamespace(mission_id=mission_id, state=self.state)

    async def transition_state(self, mission_id, new_state, **_kwargs):
        self.state = new_state
        return SimpleNamespace(seq=42)


def app_fixture(state=MissionState.RUNNING):
    bus = EventBus()
    manager = FakeManager(state)
    coordinator = MissionToolApprovalCoordinator(bus)
    app = FastAPI()
    app.include_router(router)
    app.state.mission_manager = manager
    app.state.mission_tool_approvals = coordinator
    return app, bus, manager, coordinator


async def add_pending(bus, mission="mission-1", risk="ask"):
    event = ActionApprovalRequired(
        trace_id=uuid4(), mission_id=mission, tool_name="external-action",
        risk_tier=risk, reason="risk_tier", args_preview="{}",
        expires_at_ns=time.time_ns() + 60_000_000_000,
    )
    await bus.publish(event)
    return event


# FUNCTION TESTS
@pytest.mark.asyncio
async def test_deny_mission_revokes_only_its_tickets():
    _, bus, _, coordinator = app_fixture()
    denied = []
    bus.subscribe(ActionDenied, lambda event: collect(denied, event))
    first = await add_pending(bus)
    other = await add_pending(bus, mission="mission-2")
    assert await coordinator.deny_mission("mission-1", reason="mission_cancelled") == 1
    assert await coordinator.list_pending("mission-1") == ()
    assert [x.trace_id for x in await coordinator.list_pending("mission-2")] == [other.trace_id]
    assert [(x.trace_id, x.reason) for x in denied] == [(first.trace_id, "mission_cancelled")]


async def collect(items, item):
    items.append(item)


@pytest.mark.asyncio
async def test_revoked_ticket_cannot_be_approved_again():
    _, bus, _, coordinator = app_fixture()
    ticket = await add_pending(bus)
    await coordinator.deny_mission("mission-1", reason="mission_cancelled")
    assert await coordinator.approve("mission-1", ticket.trace_id, approved_by="user") is None
    assert await coordinator.deny_mission("mission-1", reason="mission_cancelled") == 0


# INTEGRATION TESTS
@pytest.mark.asyncio
async def test_cancel_route_denies_waiter_before_returning():
    app, bus, manager, coordinator = app_fixture()
    ticket = await add_pending(bus)
    approved, denied = [], []
    bus.subscribe(ActionApproved, lambda event: collect(approved, event))
    bus.subscribe(ActionDenied, lambda event: collect(denied, event))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/missions/mission-1/cancel")
        stale = await client.post(f"/api/missions/mission-1/tool-approvals/{ticket.trace_id}/approve")
        listed = await client.get("/api/missions/mission-1/tool-approvals")
    assert response.status_code == 200
    assert manager.state == MissionState.CANCELLED
    assert stale.status_code == 409
    assert listed.json()["approvals"] == []
    assert len(approved) == 0
    assert len(denied) == 1 and denied[0].trace_id == ticket.trace_id
    assert await coordinator.list_pending("mission-1") == ()


@pytest.mark.asyncio
async def test_cancel_releases_real_approval_waiter_as_denied():
    app, bus, _, _ = app_fixture()
    workflow = ApprovalWorkflow(bus)
    trace_id = uuid4()
    ticket = workflow.arm(trace_id)
    await bus.publish(ActionApprovalRequired(
        trace_id=trace_id, mission_id="mission-1", tool_name="external-action",
        risk_tier="ask", reason="risk_tier", args_preview="{}",
        expires_at_ns=time.time_ns() + 60_000_000_000,
    ))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/missions/mission-1/cancel")
    assert response.status_code == 200
    assert await ticket.wait(0.1) == (False, "mission_cancelled")


@pytest.mark.asyncio
async def test_active_mission_approval_still_works():
    app, bus, _, _ = app_fixture(MissionState.CRITIQUING)
    ticket = await add_pending(bus)
    approved = []
    bus.subscribe(ActionApproved, lambda event: collect(approved, event))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/missions/mission-1/tool-approvals/{ticket.trace_id}/approve")
    assert response.status_code == 200
    assert len(approved) == 1 and approved[0].trace_id == ticket.trace_id


# SECURITY / REGRESSION TESTS
@pytest.mark.asyncio
@pytest.mark.parametrize("state", [MissionState.APPROVED, MissionState.FAILED, MissionState.CANCELLED, MissionState.TIMED_OUT, MissionState.WAITING_CAPACITY, MissionState.PENDING])
async def test_no_approval_when_mission_not_active(state):
    app, bus, _, _ = app_fixture(state)
    ticket = await add_pending(bus)
    approved = []
    bus.subscribe(ActionApproved, lambda event: collect(approved, event))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/missions/mission-1/tool-approvals/{ticket.trace_id}/approve")
    assert response.status_code == 409
    assert approved == []


@pytest.mark.asyncio
async def test_block_tier_cannot_be_presented_as_approvable():
    app, bus, _, coordinator = app_fixture()
    ticket = await add_pending(bus, risk="block")
    assert await coordinator.list_pending("mission-1") == ()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/missions/mission-1/tool-approvals/{ticket.trace_id}/approve")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_wrong_mission_id_does_not_revoke_foreign_ticket():
    _, bus, _, coordinator = app_fixture()
    ticket = await add_pending(bus, mission="mission-2")
    assert await coordinator.deny_mission("mission-1", reason="mission_cancelled") == 0
    assert len(await coordinator.list_pending("mission-2")) == 1
    assert (await coordinator.list_pending("mission-2"))[0].trace_id == ticket.trace_id

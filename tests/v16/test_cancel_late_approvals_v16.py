"""V16: a cancelled mission must not reopen approval gates on late events.

Uses a real in-process EventBus and ApprovalWorkflow, plus ASGI endpoints;
no production DB or external tool is used.
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
from jarvis.missions.tool_approvals import MissionToolApprovalCoordinator, MissionToolAutoApprover
from jarvis.safety.approval import ApprovalWorkflow
from jarvis.ui.web.missions_routes import router


class FakeManager:
    def __init__(self):
        self.states = {"m1": MissionState.RUNNING, "m2": MissionState.RUNNING}
        self.store = SimpleNamespace(append_and_publish=self.append)
        self.events = []

    async def append(self, event):
        self.events.append(event)

    async def mission(self, mission_id):
        state = self.states.get(mission_id)
        return SimpleNamespace(state=state) if state is not None else None

    async def transition_state(self, mission_id, new_state, **_kwargs):
        self.states[mission_id] = new_state
        return SimpleNamespace(seq=1)


def stack():
    bus = EventBus()
    coordinator = MissionToolApprovalCoordinator(bus)
    auto = MissionToolAutoApprover(bus)
    workflow = ApprovalWorkflow(bus)
    manager = FakeManager()
    app = FastAPI()
    app.include_router(router)
    app.state.mission_manager = manager
    app.state.mission_tool_approvals = coordinator
    app.state.mission_tool_auto_approver = auto
    return app, bus, coordinator, auto, workflow, manager


def required(mission_id="m1", tool_name="safe_read"):
    return ActionApprovalRequired(
        trace_id=uuid4(), mission_id=mission_id, tool_name=tool_name,
        risk_tier="ask", reason="risk_tier", args_preview="{}",
        expires_at_ns=time.time_ns() + 60_000_000_000,
    )


# Function group
@pytest.mark.asyncio
async def test_revoked_coordinator_denies_late_waiter_instead_of_parking():
    _, bus, coordinator, _, workflow, _ = stack()
    assert await coordinator.deny_mission("m1", reason="mission_cancelled") == 0
    event = required()
    ticket = workflow.arm(event.trace_id)
    await bus.publish(event)
    assert await ticket.wait(0.2) == (False, "mission_cancelled")
    assert await coordinator.list_pending("m1") == ()


@pytest.mark.asyncio
async def test_revocation_is_idempotent_and_direct_approval_fails_closed():
    _, bus, coordinator, _, _, _ = stack()
    event = required()
    await bus.publish(event)
    assert await coordinator.deny_mission("m1", reason="mission_cancelled") == 1
    assert await coordinator.deny_mission("m1", reason="mission_cancelled") == 0
    assert await coordinator.approve("m1", event.trace_id, approved_by="user") is None


# Integration group
@pytest.mark.asyncio
async def test_cancel_route_disarms_grants_and_rejects_late_event():
    app, bus, coordinator, auto, workflow, manager = stack()
    auto.arm("m1", "grant1", frozenset({"safe_read"}))
    approved = []
    async def on_approved(event):
        approved.append(event)
    bus.subscribe(ActionApproved, on_approved)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/missions/m1/cancel")
    assert response.status_code == 200
    assert manager.states["m1"] == MissionState.CANCELLED
    assert "m1" not in auto._grants
    auto.arm("m1", "late-grant", frozenset({"safe_read"}))
    assert "m1" not in auto._grants
    event = required()
    ticket = workflow.arm(event.trace_id)
    await bus.publish(event)
    assert await ticket.wait(0.2) == (False, "mission_cancelled")
    assert approved == []
    assert await coordinator.list_pending("m1") == ()


@pytest.mark.asyncio
async def test_cancel_one_mission_does_not_disable_other_grants():
    app, bus, _, auto, workflow, _ = stack()
    auto.arm("m1", "g1", frozenset({"safe_read"}))
    auto.arm("m2", "g2", frozenset({"safe_read"}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/missions/m1/cancel")
    assert response.status_code == 200
    event = required("m2")
    ticket = workflow.arm(event.trace_id)
    await bus.publish(event)
    result = await ticket.wait(0.2)
    assert result[0] is True and result[1].startswith("mission-grant:")
    assert "m2" in auto._grants


# Security / regression group
@pytest.mark.asyncio
async def test_late_denial_emitted_once_per_late_request():
    _, bus, coordinator, _, workflow, _ = stack()
    await coordinator.deny_mission("m1", reason="mission_cancelled")
    denied = []
    async def record(event):
        denied.append(event)
    bus.subscribe(ActionDenied, record)
    event = required()
    ticket = workflow.arm(event.trace_id)
    await bus.publish(event)
    assert await ticket.wait(0.2) == (False, "mission_cancelled")
    assert len([e for e in denied if e.trace_id == event.trace_id]) == 1


@pytest.mark.asyncio
async def test_preexisting_other_mission_manual_approval_unchanged():
    _, bus, coordinator, _, _, _ = stack()
    other = required("m2", tool_name="external-action")
    await bus.publish(other)
    await coordinator.deny_mission("m1", reason="mission_cancelled")
    assert await coordinator.approve("m2", other.trace_id, approved_by="user") is not None


@pytest.mark.asyncio
async def test_cannot_reapprove_late_revoked_trace_through_rest():
    app, bus, _, _, _, _ = stack()
    event = required()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/api/missions/m1/cancel")).status_code == 200
        await bus.publish(event)
        result = await client.post(f"/api/missions/m1/tool-approvals/{event.trace_id}/approve")
    assert result.status_code == 409

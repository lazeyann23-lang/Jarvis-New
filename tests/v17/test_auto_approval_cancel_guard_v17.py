"""V17: auto-approvals share the REST cancel/decision linearization guard.

Uses in-process EventBus + FastAPI ASGI routes, never executes a tool.
"""
import asyncio
import sys
import time
import types
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

# The local test image lacks aiosqlite; import shim is test-only.
if "aiosqlite" not in sys.modules:
    try:
        import aiosqlite  # noqa: F401
    except ImportError:
        shim = types.ModuleType("aiosqlite")
        for name in ("Connection", "Cursor", "Row"):
            setattr(shim, name, type(name, (), {}))
        sys.modules["aiosqlite"] = shim

from jarvis.core.bus import EventBus
from jarvis.core.events import ActionApprovalRequired, ActionApproved
from jarvis.missions.state_machine import MissionState
from jarvis.missions.tool_approvals import (
    MissionToolApprovalCoordinator,
    MissionToolAutoApprover,
)
from jarvis.ui.web.missions_routes import router


class Manager:
    def __init__(self):
        self.states = {"m1": MissionState.RUNNING, "m2": MissionState.RUNNING}
        self.transition_entered = asyncio.Event()
        self.allow_transition = asyncio.Event()
        self.allow_transition.set()
        self.store = SimpleNamespace(append_and_publish=self.append)

    async def append(self, _event):
        pass

    async def mission(self, mission_id):
        state = self.states.get(mission_id)
        return SimpleNamespace(state=state) if state is not None else None

    async def transition_state(self, mission_id, new_state, **_kwargs):
        self.transition_entered.set()
        await self.allow_transition.wait()
        self.states[mission_id] = new_state
        return SimpleNamespace(seq=1)


def stack():
    bus = EventBus()
    coordinator = MissionToolApprovalCoordinator(bus)
    auto = MissionToolAutoApprover(bus, coordinator=coordinator)
    manager = Manager()
    app = FastAPI()
    app.include_router(router)
    app.state.mission_manager = manager
    app.state.mission_tool_approvals = coordinator
    app.state.mission_tool_auto_approver = auto
    return app, bus, coordinator, auto, manager


def required(mission="m1", tool="external-action", risk="ask"):
    return ActionApprovalRequired(
        trace_id=uuid4(), mission_id=mission, tool_name=tool,
        risk_tier=risk, reason="risk_tier", args_preview="{}",
        expires_at_ns=time.time_ns() + 60_000_000_000,
    )


# Function tests
@pytest.mark.asyncio
async def test_auto_approves_armed_grant_with_shared_guard():
    _, bus, _, auto, _ = stack()
    auto.arm("m1", "g1", frozenset({"external-action"}))
    approvals = []
    bus.subscribe(ActionApproved, lambda event: record(approvals, event))
    event = required()
    await bus.publish(event)
    assert [a.trace_id for a in approvals] == [event.trace_id]


async def record(events, event):
    events.append(event)


@pytest.mark.asyncio
async def test_block_tier_and_unarmed_tools_still_fail_closed():
    _, bus, _, auto, _ = stack()
    auto.arm("m1", "g1", frozenset({"external-action"}))
    approvals = []
    bus.subscribe(ActionApproved, lambda event: record(approvals, event))
    await bus.publish(required(risk="block"))
    await bus.publish(required(tool="not-granted"))
    assert approvals == []


# Integration tests: real HTTP cancel endpoint, controlled event scheduling
@pytest.mark.asyncio
async def test_cancel_guard_wins_then_inflight_request_is_denied():
    app, bus, coord, auto, manager = stack()
    auto.arm("m1", "g1", frozenset({"external-action"}))
    manager.allow_transition.clear()
    approvals = []
    bus.subscribe(ActionApproved, lambda event: record(approvals, event))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        cancel = asyncio.create_task(client.post("/api/missions/m1/cancel"))
        await asyncio.wait_for(manager.transition_entered.wait(), 2)
        publish = asyncio.create_task(bus.publish(required()))
        await asyncio.sleep(0.02)
        assert not publish.done()  # auto-handler waits for cancellation's guard
        manager.allow_transition.set()
        assert (await asyncio.wait_for(cancel, 2)).status_code == 200
        await asyncio.wait_for(publish, 2)
    assert approvals == []
    assert await coord.list_pending("m1") == ()


@pytest.mark.asyncio
async def test_auto_approval_wins_then_cancel_waits_until_publish_finishes():
    app, bus, _, auto, manager = stack()
    auto.arm("m1", "g1", frozenset({"external-action"}))
    approval_entered = asyncio.Event()
    release_approval = asyncio.Event()
    approvals = []

    async def hold_approved(event):
        approval_entered.set()
        await release_approval.wait()
        approvals.append(event)

    bus.subscribe(ActionApproved, hold_approved)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        publish = asyncio.create_task(bus.publish(required()))
        await asyncio.wait_for(approval_entered.wait(), 2)
        cancel = asyncio.create_task(client.post("/api/missions/m1/cancel"))
        await asyncio.sleep(0.02)
        assert not manager.transition_entered.is_set()
        release_approval.set()
        await asyncio.wait_for(publish, 2)
        assert (await asyncio.wait_for(cancel, 2)).status_code == 200
    assert len(approvals) == 1
    assert manager.states["m1"] == MissionState.CANCELLED


# Security / regression tests
@pytest.mark.asyncio
async def test_late_events_and_rearming_after_cancel_never_approve():
    app, bus, _, auto, _ = stack()
    auto.arm("m1", "g1", frozenset({"external-action"}))
    approvals = []
    bus.subscribe(ActionApproved, lambda event: record(approvals, event))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/api/missions/m1/cancel")).status_code == 200
    auto.arm("m1", "g2", frozenset({"external-action"}))
    await bus.publish(required())
    assert approvals == []
    assert "m1" not in auto._grants


@pytest.mark.asyncio
async def test_one_mission_waiting_does_not_block_another_mission():
    _, bus, _, auto, _ = stack()
    auto.arm("m1", "g1", frozenset({"external-action"}))
    auto.arm("m2", "g2", frozenset({"external-action"}))
    entered = asyncio.Event()
    release = asyncio.Event()
    first = required("m1")
    second = required("m2")
    approved = []

    async def observe(event):
        if event.trace_id == first.trace_id:
            entered.set()
            await release.wait()
        approved.append(event.trace_id)

    bus.subscribe(ActionApproved, observe)
    task = asyncio.create_task(bus.publish(first))
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(bus.publish(second), 2)
    assert second.trace_id in approved
    release.set()
    await asyncio.wait_for(task, 2)
    assert set(approved) == {first.trace_id, second.trace_id}


@pytest.mark.asyncio
async def test_guard_objects_are_released_after_auto_approvals():
    _, bus, coordinator, auto, _ = stack()
    for i in range(40):
        mission = f"m-{i}"
        auto.arm(mission, "g", frozenset({"external-action"}))
        await bus.publish(required(mission))
    assert len(coordinator._mission_decision_locks) <= 1

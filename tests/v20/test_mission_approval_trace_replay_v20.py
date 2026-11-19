"""V20: replay/duplicate approval-trace safety in the mission coordinator.

All tests use the actual in-process EventBus. No production DB is implied.
"""
from __future__ import annotations

import asyncio
import time
from uuid import UUID, uuid4

import pytest

from jarvis.core.bus import EventBus
from jarvis.core.events import ActionApprovalRequired, ActionApproved, ActionDenied
from jarvis.missions.tool_approvals import MissionToolApprovalCoordinator
from jarvis.safety.approval import ApprovalWorkflow


def request(*, trace_id: UUID | None = None, mission: str = "mission-a", tool: str = "mail/send") -> ActionApprovalRequired:
    return ActionApprovalRequired(
        trace_id=trace_id or uuid4(),
        mission_id=mission,
        worker_id="worker-a",
        tool_name=tool,
        risk_tier="ask",
        reason="risk_tier",
        args_preview="<redacted>",
        expires_at_ns=time.time_ns() + 60_000_000_000,
    )


@pytest.mark.asyncio
async def test_function_duplicate_pending_keeps_first_request() -> None:
    bus = EventBus()
    coordinator = MissionToolApprovalCoordinator(bus)
    original = request()
    await bus.publish(original)
    await bus.publish(request(trace_id=original.trace_id, tool="shell/execute"))
    pending = await coordinator.list_pending("mission-a")
    assert len(pending) == 1
    assert pending[0].tool_name == "mail/send"
    assert pending[0].args_preview == "<redacted>"


@pytest.mark.asyncio
async def test_function_approved_trace_cannot_be_reopened() -> None:
    bus = EventBus()
    coordinator = MissionToolApprovalCoordinator(bus)
    original = request()
    await bus.publish(original)
    assert await coordinator.approve("mission-a", original.trace_id, approved_by="user") is not None
    await bus.publish(original)
    assert await coordinator.list_pending("mission-a") == ()
    assert await coordinator.approve("mission-a", original.trace_id, approved_by="user") is None


@pytest.mark.asyncio
async def test_function_denied_trace_cannot_be_reopened() -> None:
    bus = EventBus()
    coordinator = MissionToolApprovalCoordinator(bus)
    original = request()
    await bus.publish(original)
    assert await coordinator.deny("mission-a", original.trace_id, reason="user_denied") is not None
    await bus.publish(original)
    assert await coordinator.list_pending("mission-a") == ()


@pytest.mark.asyncio
async def test_integration_early_autoapproval_during_state_read_stays_closed() -> None:
    bus = EventBus()
    early_approved = asyncio.Event()

    async def delayed_status(_mission: str) -> bool:
        await asyncio.wait_for(early_approved.wait(), timeout=2)
        return True

    coordinator = MissionToolApprovalCoordinator(bus, mission_is_active=delayed_status)

    async def autoapprove(event: ActionApprovalRequired) -> None:
        await bus.publish(ActionApproved(trace_id=event.trace_id, tool_name=event.tool_name, approved_by="mission-grant"))
        early_approved.set()

    bus.subscribe(ActionApprovalRequired, autoapprove)
    original = request()
    await asyncio.wait_for(bus.publish(original), timeout=3)
    assert await coordinator.list_pending("mission-a") == ()
    await bus.publish(original)
    assert await coordinator.list_pending("mission-a") == ()


@pytest.mark.asyncio
async def test_integration_real_approval_workflow_first_wins_and_no_ui_replay() -> None:
    bus = EventBus()
    coordinator = MissionToolApprovalCoordinator(bus)
    workflow = ApprovalWorkflow(bus)
    original = request()
    ticket = workflow.arm(original.trace_id)
    await bus.publish(original)
    assert await coordinator.approve("mission-a", original.trace_id, approved_by="user") is not None
    assert ticket.peek() == (True, "user")
    await bus.publish(original)
    await bus.publish(ActionDenied(trace_id=original.trace_id, tool_name=original.tool_name, reason="late_denial"))
    assert ticket.peek() == (True, "user")
    assert await coordinator.list_pending("mission-a") == ()
    ticket.close()


@pytest.mark.asyncio
async def test_security_cross_mission_replay_cannot_hijack_trace() -> None:
    bus = EventBus()
    coordinator = MissionToolApprovalCoordinator(bus)
    original = request(mission="mission-a")
    await bus.publish(original)
    await bus.publish(request(trace_id=original.trace_id, mission="mission-b", tool="shell/execute"))
    assert await coordinator.list_pending("mission-b") == ()
    assert await coordinator.approve("mission-b", original.trace_id, approved_by="user") is None
    pending = await coordinator.list_pending("mission-a")
    assert len(pending) == 1 and pending[0].tool_name == "mail/send"


@pytest.mark.asyncio
async def test_security_replay_tombstone_cache_has_hard_bound(monkeypatch) -> None:
    import jarvis.missions.tool_approvals as mod
    monkeypatch.setattr(mod, "_MAX_RESOLVED_TRACES", 3)
    bus = EventBus()
    coordinator = MissionToolApprovalCoordinator(bus)
    for _ in range(15):
        await bus.publish(ActionDenied(trace_id=uuid4(), tool_name="test", reason="blocked"))
    assert len(coordinator._resolved_traces) == 3
    # The most recent trace is still protected, including against an
    # unsolicited replay after its denial arrived before the request.
    recent = next(reversed(coordinator._resolved_traces))
    await bus.publish(request(trace_id=recent))
    assert await coordinator.list_pending("mission-a") == ()


@pytest.mark.asyncio
async def test_security_trace_closed_before_async_approval_publish_finishes() -> None:
    bus = EventBus()
    coordinator = MissionToolApprovalCoordinator(bus)
    original = request()
    await bus.publish(original)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed_observer(event: ActionApproved) -> None:
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=2)

    bus.subscribe(ActionApproved, delayed_observer)
    task = asyncio.create_task(coordinator.approve("mission-a", original.trace_id, approved_by="user"))
    await asyncio.wait_for(entered.wait(), timeout=2)
    await bus.publish(original)
    assert await coordinator.list_pending("mission-a") == ()
    release.set()
    assert await asyncio.wait_for(task, timeout=2) is not None


@pytest.mark.asyncio
async def test_security_cancel_then_replay_is_not_actionable() -> None:
    bus = EventBus()
    coordinator = MissionToolApprovalCoordinator(bus)
    original = request()
    await bus.publish(original)
    assert await coordinator.deny_mission("mission-a", reason="mission_cancelled") == 1
    await bus.publish(original)
    assert await coordinator.list_pending("mission-a") == ()
    assert await coordinator.approve("mission-a", original.trace_id, approved_by="user") is None

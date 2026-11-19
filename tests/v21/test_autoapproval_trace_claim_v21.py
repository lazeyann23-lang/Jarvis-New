"""V21: one-shot automatic approval of a trace on the real local EventBus.

No external tool is executed and no production database is used.
"""
from __future__ import annotations

import asyncio
import time
from uuid import UUID, uuid4

import pytest

from jarvis.core.bus import EventBus
from jarvis.core.events import ActionApprovalRequired, ActionApproved, ActionDenied
from jarvis.missions.tool_approvals import MissionToolApprovalCoordinator, MissionToolAutoApprover
from jarvis.safety.approval import ApprovalWorkflow


def required(*, trace_id: UUID | None = None, mission: str = "m1", tool: str = "external-action", expires: int | None = None):
    return ActionApprovalRequired(
        trace_id=trace_id or uuid4(), mission_id=mission, worker_id="w1",
        tool_name=tool, risk_tier="ask", reason="risk_tier",
        args_preview="<redacted>",
        expires_at_ns=expires if expires is not None else time.time_ns() + 60_000_000_000,
    )


def stack():
    bus = EventBus()
    coordinator = MissionToolApprovalCoordinator(bus)
    auto = MissionToolAutoApprover(bus, coordinator=coordinator)
    approvals: list[ActionApproved] = []

    async def collect(event):
        approvals.append(event)

    bus.subscribe(ActionApproved, collect)
    return bus, coordinator, auto, approvals


@pytest.mark.asyncio
async def test_function_automatic_trace_is_consumed_once():
    bus, coordinator, auto, approvals = stack()
    auto.arm("m1", "grant", frozenset({"external-action"}))
    event = required()
    await bus.publish(event)
    await bus.publish(event)
    assert [item.trace_id for item in approvals] == [event.trace_id]
    assert await coordinator.list_pending("m1") == ()


@pytest.mark.asyncio
async def test_function_expired_request_is_not_auto_approved():
    bus, coordinator, auto, approvals = stack()
    auto.arm("m1", "grant", frozenset({"external-action"}))
    await bus.publish(required(expires=time.time_ns() - 1))
    assert approvals == []
    assert await coordinator.list_pending("m1") == ()


@pytest.mark.asyncio
async def test_integration_real_workflow_resolves_once_and_does_not_reopen():
    bus, coordinator, auto, approvals = stack()
    workflow = ApprovalWorkflow(bus)
    auto.arm("m1", "grant", frozenset({"external-action"}))
    event = required()
    ticket = workflow.arm(event.trace_id)
    try:
        await bus.publish(event)
        assert ticket.peek()[0] is True
        await bus.publish(event)
        assert len(approvals) == 1
        assert await coordinator.list_pending("m1") == ()
    finally:
        ticket.close()


@pytest.mark.asyncio
async def test_integration_auto_resolves_existing_pending_ticket():
    bus, coordinator, auto, approvals = stack()
    event = required()
    await bus.publish(event)
    assert len(await coordinator.list_pending("m1")) == 1
    auto.arm("m1", "grant", frozenset({"external-action"}))
    await bus.publish(event)
    assert len(approvals) == 1
    assert await coordinator.list_pending("m1") == ()


@pytest.mark.asyncio
async def test_security_denied_trace_cannot_be_auto_approved_by_replay():
    bus, coordinator, auto, approvals = stack()
    event = required()
    await bus.publish(event)
    assert await coordinator.deny("m1", event.trace_id, reason="user_denied") is not None
    auto.arm("m1", "grant", frozenset({"external-action"}))
    await bus.publish(event)
    assert approvals == []


@pytest.mark.asyncio
async def test_security_trace_collision_between_missions_cannot_auto_approve():
    bus, coordinator, auto, approvals = stack()
    original = required(mission="m2", tool="other-tool")
    await bus.publish(original)
    auto.arm("m1", "grant", frozenset({"external-action"}))
    await bus.publish(required(trace_id=original.trace_id, mission="m1"))
    assert approvals == []
    assert len(await coordinator.list_pending("m2")) == 1


@pytest.mark.asyncio
async def test_security_concurrent_duplicate_events_emit_single_approval():
    bus, coordinator, auto, approvals = stack()
    auto.arm("m1", "grant", frozenset({"external-action"}))
    event = required()
    await asyncio.wait_for(asyncio.gather(bus.publish(event), bus.publish(event)), timeout=3)
    assert len(approvals) == 1
    assert await coordinator.list_pending("m1") == ()


@pytest.mark.asyncio
async def test_security_manual_decision_racing_autoapproval_is_single_winner():
    bus, coordinator, auto, approvals = stack()
    event = required()
    await bus.publish(event)
    auto.arm("m1", "grant", frozenset({"external-action"}))
    denials: list[ActionDenied] = []

    async def collect(event):
        denials.append(event)

    bus.subscribe(ActionDenied, collect)
    await asyncio.wait_for(asyncio.gather(
        bus.publish(event), coordinator.deny("m1", event.trace_id, reason="user_denied")
    ), timeout=3)
    assert len(approvals) + len(denials) == 1
    assert await coordinator.list_pending("m1") == ()

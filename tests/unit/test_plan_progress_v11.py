"""Version 11: real event projection, coordinator integration, privacy regression."""
from __future__ import annotations

import time
from uuid import uuid4

import pytest

from jarvis.core.bus import EventBus
from jarvis.core.events import ActionApprovalRequired, ActionApproved
from jarvis.missions.events import (
    CriticVerdictReady,
    EventEnvelope,
    MissionPlanReady,
    WorkerDraftReady,
    WorkerKilled,
    WorkerProgress,
    WorkerSpawned,
)
from jarvis.missions.plan_progress import build_mission_progress
from jarvis.missions.tool_approvals import MissionToolApprovalCoordinator

MID = "mission-1"


def env(payload, *, worker_id=None):
    return EventEnvelope(mission_id=MID, source_actor="system", ts_ms=1,
                         worker_id=worker_id, payload=payload)


def plan():
    return env(MissionPlanReady(plan=[
        {"task_id": "t1", "slug": "research", "prompt": "SECRET_PASSWORD_123"},
        {"task_id": "t2", "slug": "write-report", "prompt": "private client data"},
    ], n_workers=2))


def spawned(task, worker):
    return env(WorkerSpawned(worker_id=worker, step={"task_id": task}, pid=1,
                             cli="python", model="local", worktree="/tmp/test"),
               worker_id=worker)


def test_function_multistep_tracks_real_evidence():
    events = [plan(), spawned("t1", "w1"), spawned("t2", "w2"),
              env(WorkerDraftReady(worker_id="w1", artifact_uri="x", diff="", tokens_used=0,
                                   cost_usd=0, session_id="s"), worker_id="w1"),
              env(CriticVerdictReady(worker_id="w1", verdict="approve", summary="ok",
                                     confidence=1, iteration=1), worker_id="w1"),
              env(WorkerProgress(worker_id="w2", note="working"), worker_id="w2")]
    result = build_mission_progress(MID, "RUNNING", events)
    assert result["total_steps"] == 2
    assert [s["status"] for s in result["steps"]] == ["completed", "running"]
    assert result["counts"]["completed"] == 1
    assert not result["requires_user_action"]


def test_function_failed_and_parked_steps_never_claim_success():
    events = [plan(), spawned("t1", "w1"),
              env(WorkerKilled(worker_id="w1", reason="worker_error"), worker_id="w1")]
    parked = build_mission_progress(MID, "WAITING_CAPACITY", events)
    assert [s["status"] for s in parked["steps"]] == ["failed", "waiting_capacity"]
    assert parked["requires_user_action"]
    terminal = build_mission_progress(MID, "APPROVED", [plan()])
    assert all(s["status"] == "unverified" for s in terminal["steps"])


@pytest.mark.asyncio
async def test_integration_bus_approval_blocks_step_until_explicit_decision():
    bus = EventBus()
    coordinator = MissionToolApprovalCoordinator(bus)
    required = ActionApprovalRequired(
        trace_id=uuid4(), tool_name="gmail/send_message", risk_tier="ask",
        reason="risk_tier", args_preview="REDACTED", mission_id=MID,
        worker_id="w2", expires_at_ns=time.time_ns() + 30_000_000_000,
    )
    await bus.publish(required)
    pending = await coordinator.list_pending(MID)
    result = build_mission_progress(MID, "RUNNING", [plan(), spawned("t2", "w2")], pending)
    assert result["requires_user_action"]
    assert result["steps"][1]["status"] == "waiting_approval"
    assert result["pending_approvals"] == 1
    assert result["steps"][1]["approval_count"] == 1
    assert await coordinator.approve("wrong-mission", required.trace_id, approved_by="user") is None
    assert len(await coordinator.list_pending(MID)) == 1
    assert await coordinator.approve(MID, required.trace_id, approved_by="user") is not None
    assert (await coordinator.list_pending(MID)) == ()
    after = build_mission_progress(MID, "RUNNING", [plan(), spawned("t2", "w2")])
    assert after["steps"][1]["status"] == "running"


@pytest.mark.asyncio
async def test_integration_unknown_worker_approval_still_requires_user_action():
    bus = EventBus()
    coordinator = MissionToolApprovalCoordinator(bus)
    await bus.publish(ActionApprovalRequired(
        trace_id=uuid4(), tool_name="browser/open", risk_tier="ask",
        reason="risk_tier", mission_id=MID, worker_id="other",
        expires_at_ns=time.time_ns() + 30_000_000_000,
    ))
    result = build_mission_progress(MID, "RUNNING", [plan()], await coordinator.list_pending(MID))
    assert result["pending_approvals"] == 1
    assert result["requires_user_action"]
    assert all(step["status"] == "queued" for step in result["steps"])


def test_security_no_prompts_or_tool_arguments_in_projection():
    result = build_mission_progress(MID, "RUNNING", [plan()])
    text = repr(result)
    assert "SECRET_PASSWORD_123" not in text
    assert "private client data" not in text
    assert "prompt" not in text
    assert "args_preview" not in text


def test_regression_no_plan_keeps_empty_read_model():
    result = build_mission_progress(MID, "PENDING", [])
    assert result["plan_available"] is False
    assert result["steps"] == []
    assert result["total_steps"] == 0


def test_security_bounded_duplicate_and_foreign_approval():
    too_many = [{"task_id": f"task-{i}", "slug": "a" * 200, "prompt": "secret"}
                for i in range(20)]
    events = [env(MissionPlanReady(plan=too_many, n_workers=5))]
    result = build_mission_progress(MID, "RUNNING", events)
    assert result["total_steps"] == 5
    assert all(len(s["label"]) <= 80 for s in result["steps"])

"""Read-only, secret-free progress projection for multi-step missions.

The planner and tool executor remain the authorities. This module does not
approve tools, launch workers or infer success from an unverified transcript.
It projects durable mission events plus the current approval queue into a
bounded view suitable for desktop and mobile clients.
"""
from __future__ import annotations

from collections.abc import Sequence
import re
from typing import Any

from jarvis.missions.events import EventEnvelope
from jarvis.missions.tool_approvals import PendingMissionToolApproval

_TERMINAL = {"APPROVED", "FAILED", "CANCELLED", "TIMED_OUT"}


def build_mission_progress(
    mission_id: str,
    mission_state: str,
    events: Sequence[EventEnvelope],
    pending: Sequence[PendingMissionToolApproval] = (),
) -> dict[str, Any]:
    """Summarize *observed* work; do not expose step prompts or tool args.

    The latest plan wins on a retry. Per-worker status is tied to the task_id
    from WorkerSpawned.step, not a position guess. Pending approvals take
    precedence over generic worker progress and remain explicitly user-gated.
    """
    plan = None
    plan_at = -1
    for index, env in enumerate(events):
        if env.payload.event_type == "MissionPlanReady":
            plan = env.payload
            plan_at = index

    steps: list[dict[str, Any]] = []
    by_task: dict[str, dict[str, Any]] = {}
    worker_to_task: dict[str, str] = {}
    if plan is not None:
        for index, raw in enumerate(plan.plan[:5]):
            if not isinstance(raw, dict):
                continue
            task_id = str(raw.get("task_id") or "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", task_id) or task_id in by_task:
                continue
            # Planner fields can themselves contain private user text: expose
            # only simple identifier-shaped slugs, never arbitrary prose.
            slug = str(raw.get("slug") or "")
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", slug):
                slug = f"step-{index + 1}"
            step: dict[str, Any] = {
                "task_id": task_id,
                "label": slug,
                "status": "queued",
                "worker_id": None,
                "approval_count": 0,
            }
            steps.append(step)
            by_task[task_id] = step

    for env in events[plan_at + 1:]:
        event = env.payload
        kind = event.event_type
        if kind == "WorkerSpawned":
            task_id = str(event.step.get("task_id") or "")
            if task_id in by_task:
                worker_to_task[event.worker_id] = task_id
                step = by_task[task_id]
                step["worker_id"] = event.worker_id
                step["status"] = "running"
        elif kind in {"WorkerProgress", "WorkerDraftReady", "CriticVerdictReady", "WorkerKilled", "WorkerCorrectionRequired"}:
            task_id = worker_to_task.get(event.worker_id)
            if not task_id:
                continue
            step = by_task[task_id]
            if kind == "WorkerProgress":
                if step["status"] not in {"completed", "failed"}:
                    step["status"] = "running"
            elif kind == "WorkerDraftReady":
                step["status"] = "reviewing"
            elif kind == "CriticVerdictReady":
                step["status"] = {
                    "approve": "completed", "revise": "revising", "reject": "failed"
                }[event.verdict]
            elif kind == "WorkerCorrectionRequired":
                step["status"] = "revising"
            else:
                step["status"] = "failed"

    # Approval gates belong to the ToolExecutor. This projection only makes
    # them visible and never treats a requested approval as consent.
    for approval in pending:
        if approval.mission_id != mission_id:
            continue
        task_id = worker_to_task.get(approval.worker_id or "")
        if task_id is not None:
            step = by_task[task_id]
            step["approval_count"] += 1
            step["status"] = "waiting_approval"

    pending_count = sum(1 for approval in pending if approval.mission_id == mission_id)
    if mission_state == "WAITING_CAPACITY":
        for step in steps:
            if step["status"] in {"queued", "running", "reviewing", "revising"}:
                step["status"] = "waiting_capacity"
    elif mission_state in _TERMINAL:
        for step in steps:
            if step["status"] in {"queued", "running", "reviewing", "revising", "waiting_approval"}:
                # Never claim a step succeeded merely because the overall
                # mission reached a terminal state.
                step["status"] = "unverified" if mission_state == "APPROVED" else "not_completed"

    counts = {status: sum(s["status"] == status for s in steps)
              for status in ("queued", "running", "reviewing", "revising", "completed",
                             "failed", "waiting_approval", "waiting_capacity", "unverified", "not_completed")}
    return {
        "mission_id": mission_id,
        "mission_state": mission_state,
        "plan_available": plan is not None,
        "total_steps": len(steps),
        "steps": steps,
        "counts": counts,
        "pending_approvals": pending_count,
        "requires_user_action": pending_count > 0 or mission_state == "WAITING_CAPACITY",
        "note": "Statuses reflect recorded events, not an independent verification of worker output.",
    }

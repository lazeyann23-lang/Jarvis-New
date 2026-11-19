"""Mission-scoped view and decision bridge for paused supervisor tool calls."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
import threading
import time
from dataclasses import asdict, dataclass
from uuid import UUID
from weakref import WeakValueDictionary

from loguru import logger

# Resolved traces must not be re-opened by duplicate/replayed bus events.
# ToolExecutor caps its approval wait at 15 minutes. Keep a bounded cache
# covering that window; this is process-local replay protection, not a
# distributed transaction or a permanent audit log.
_RESOLVED_TRACE_TTL_NS = 15 * 60 * 1_000_000_000
_MAX_RESOLVED_TRACES = 8192

from jarvis.core.bus import EventBus
from jarvis.core.events import (
    ActionApprovalRequired,
    ActionApproved,
    ActionDenied,
    ActionExecuted,
)


@dataclass(frozen=True, slots=True)
class PendingMissionToolApproval:
    """Secret-free data needed to render and decide one paused call."""

    trace_id: UUID
    mission_id: str
    worker_id: str | None
    tool_name: str
    risk_tier: str
    reason: str
    args_preview: str
    requested_at_ns: int
    expires_at_ns: int

    def to_dict(self) -> dict[str, str | int | None]:
        payload = asdict(self)
        payload["trace_id"] = str(self.trace_id)
        return payload


class MissionToolApprovalCoordinator:
    """Tracks mission approvals while ``ApprovalWorkflow`` owns the waiter.

    The coordinator never holds tool objects, arguments, or credentials. It is
    an in-memory projection of bus events and publishes the normal
    ``ActionApproved``/``ActionDenied`` decisions, preserving the single safety
    path through ``ToolExecutor``.
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        mission_is_active: Callable[[str], Awaitable[bool]] | None = None,
        durable_claim: Callable[[UUID, str, str, str], Awaitable[bool]] | None = None,
    ) -> None:
        self._bus = bus
        # Production reads the persisted MissionManager state. This is a
        # cross-restart/cross-process *check*, not a distributed transaction.
        # Standalone coordinators without a manager preserve legacy behavior.
        self._mission_is_active = mission_is_active
        # Production writes a SQLite first-writer-wins decision before bus
        # publication.  Standalone test/legacy coordinators may omit it.
        self._durable_claim = durable_claim
        self._pending: dict[UUID, PendingMissionToolApproval] = {}
        self._resolved_traces: OrderedDict[UUID, int] = OrderedDict()
        # Cancellation tombstones prevent late worker approval requests from
        # reopening a cancelled mission's human approval gate. Mission IDs
        # are unique and never reused. Keep them for this process lifetime:
        # dropping a tombstone on a timer would reopen a late worker request.
        self._revoked_missions: set[str] = set()
        self._lock = asyncio.Lock()
        # Per-mission locks serialize the REST status check + approval event
        # with the cancel transition + revocation. Weak references avoid
        # accumulating locks for completed missions. A caller keeps a strong
        # reference throughout its context, including while waiting.
        self._mission_decision_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        bus.subscribe(ActionApprovalRequired, self._on_required)
        bus.subscribe(ActionApproved, self._on_resolved)
        bus.subscribe(ActionDenied, self._on_resolved)
        bus.subscribe(ActionExecuted, self._on_resolved)

    @asynccontextmanager
    async def mission_decision_guard(self, mission_id: str):
        """Serialize a mission's REST approval/denial with REST cancellation.

        The caller must hold this guard while reading the authoritative state
        AND publishing the approval or committing cancellation + revocation.
        This is an in-process API ordering guarantee, NOT a distributed
        transaction, nor a cancellation barrier for already-running tools.
        """
        lock = self._mission_decision_locks.get(mission_id)
        if lock is None:
            # No await between lookup and insertion: safe on one event loop.
            lock = asyncio.Lock()
            self._mission_decision_locks[mission_id] = lock
        async with lock:
            yield

    async def mission_is_active(self, mission_id: str) -> bool:
        """Fail closed if the authoritative mission-state reader is unavailable.

        A restarted server has no in-memory cancellation tombstones. Reading
        the persisted state prevents it from re-opening approvals for a
        mission already marked terminal or paused by another instance.
        """
        if self._mission_is_active is None:
            return True  # Compatibility for independent/legacy coordinators.
        try:
            return bool(await self._mission_is_active(mission_id))
        except Exception:  # noqa: BLE001
            logger.exception("Mission state check failed; withholding tool approval")
            return False

    def _prune_resolved(self, now_ns: int) -> None:
        """Discard expired replay tombstones; bound hostile event floods."""
        while self._resolved_traces:
            _, expires_ns = next(iter(self._resolved_traces.items()))
            if expires_ns > now_ns:
                break
            self._resolved_traces.popitem(last=False)
        while len(self._resolved_traces) > _MAX_RESOLVED_TRACES:
            self._resolved_traces.popitem(last=False)

    def _mark_resolved(self, trace_id: UUID, now_ns: int) -> None:
        """Call under ``_lock`` to make a consumed trace non-reusable."""
        self._resolved_traces.pop(trace_id, None)
        self._resolved_traces[trace_id] = now_ns + _RESOLVED_TRACE_TTL_NS
        self._prune_resolved(now_ns)

    async def _on_required(self, event: ActionApprovalRequired) -> None:
        mission_id = str(event.mission_id or "").strip()
        # A blocked tool is never a user-approvable mission action. The
        # executor normally refuses it before this event, but fail closed if
        # a different publisher or replay emits a malformed request.
        if (
            not mission_id
            or event.risk_tier == "block"
            or event.expires_at_ns <= time.time_ns()
        ):
            return
        pending = PendingMissionToolApproval(
            trace_id=event.trace_id,
            mission_id=mission_id,
            worker_id=event.worker_id,
            tool_name=event.tool_name,
            risk_tier=event.risk_tier,
            reason=event.reason,
            args_preview=event.args_preview,
            requested_at_ns=event.timestamp_ns,
            expires_at_ns=event.expires_at_ns,
        )
        # A persisted terminal/paused state remains authoritative even after
        # a process restart clears the in-memory cancellation tombstones.
        active = await self.mission_is_active(mission_id)
        async with self._lock:
            self._prune_resolved(time.time_ns())
            # First request wins: never replace a pending trace's tool,
            # mission, or preview, and never re-arm a resolved trace. An
            # automatic approver may resolve it while our async persisted-
            # status check is still running (EventBus dispatches in parallel).
            if event.trace_id in self._resolved_traces or event.trace_id in self._pending:
                return
            revoked = mission_id in self._revoked_missions or not active
            if not revoked:
                self._pending[event.trace_id] = pending
        if revoked:
            # The workflow may have armed its waiter before this late event.
            # Resolve it as denied instead of leaving a hidden, timed-out gate.
            await self._bus.publish(
                ActionDenied(
                    trace_id=event.trace_id,
                    tool_name=event.tool_name,
                    reason="mission_cancelled",
                )
            )

    async def _on_resolved(
        self,
        event: ActionApproved | ActionDenied | ActionExecuted,
    ) -> None:
        async with self._lock:
            self._pending.pop(event.trace_id, None)
            # This also covers approvals arriving *before* _on_required
            # finishes its awaited mission-state check.
            self._mark_resolved(event.trace_id, time.time_ns())

    async def claim_automatic_trace(self, event: ActionApprovalRequired) -> bool:
        """Consume an authorized auto-approval trace exactly once per process.

        Called only after grant, risk, expiry and persisted mission-state checks.
        The coordinator lock arbitrates duplicate requests and races with a
        manual decision; marking before publish prevents a replay from sending
        another ActionApproved while EventBus subscribers are still running.
        This is not durable or a cross-process transaction.
        """
        now_ns = time.time_ns()
        mission_id = str(event.mission_id or "").strip()
        if not mission_id or event.expires_at_ns <= now_ns:
            return False
        async with self._lock:
            self._prune_resolved(now_ns)
            if event.trace_id in self._resolved_traces:
                return False
            pending = self._pending.get(event.trace_id)
            if pending is not None and (
                pending.mission_id != mission_id
                or pending.tool_name != event.tool_name
                or pending.expires_at_ns <= now_ns
            ):
                return False
            if self._durable_claim is not None:
                try:
                    claimed = await self._durable_claim(
                        event.trace_id, mission_id, event.tool_name, "auto_approved"
                    )
                except Exception:  # noqa: BLE001 - fail closed on DB errors
                    logger.exception("Durable mission auto-approval claim failed")
                    return False
                if not claimed:
                    self._pending.pop(event.trace_id, None)
                    self._mark_resolved(event.trace_id, now_ns)
                    return False
            self._pending.pop(event.trace_id, None)
            self._mark_resolved(event.trace_id, now_ns)
            return True

    async def list_pending(
        self,
        mission_id: str,
    ) -> tuple[PendingMissionToolApproval, ...]:
        now_ns = time.time_ns()
        async with self._lock:
            expired = [
                trace_id
                for trace_id, item in self._pending.items()
                if item.expires_at_ns <= now_ns
            ]
            for trace_id in expired:
                self._pending.pop(trace_id, None)
            return tuple(
                sorted(
                    (
                        item
                        for item in self._pending.values()
                        if item.mission_id == mission_id
                    ),
                    key=lambda item: item.requested_at_ns,
                )
            )

    async def approve(
        self,
        mission_id: str,
        trace_id: UUID,
        *,
        approved_by: str,
    ) -> PendingMissionToolApproval | None:
        pending = await self._take_live(mission_id, trace_id, decision="approved")
        if pending is None:
            return None
        # A different server process may cancel the mission *after* the
        # durable first-writer-wins claim has committed. Recheck the persisted
        # lifecycle immediately before publishing a positive decision. This
        # closes the observable claim->publish gap when cancellation has
        # already committed, but is not a distributed atomic transaction.
        if (
            mission_id in self._revoked_missions
            or not await self.mission_is_active(mission_id)
        ):
            await self._bus.publish(
                ActionDenied(
                    trace_id=trace_id,
                    tool_name=pending.tool_name,
                    reason="mission_inactive_after_claim",
                )
            )
            return None
        await self._bus.publish(
            ActionApproved(
                trace_id=trace_id,
                tool_name=pending.tool_name,
                approved_by=approved_by,
            )
        )
        return pending

    async def deny(
        self,
        mission_id: str,
        trace_id: UUID,
        *,
        reason: str,
    ) -> PendingMissionToolApproval | None:
        pending = await self._take_live(mission_id, trace_id, decision="denied")
        if pending is None:
            return None
        await self._bus.publish(
            ActionDenied(
                trace_id=trace_id,
                tool_name=pending.tool_name,
                reason=reason,
            )
        )
        return pending

    async def _take_live(
        self,
        mission_id: str,
        trace_id: UUID,
        *,
        decision: str,
    ) -> PendingMissionToolApproval | None:
        async with self._lock:
            pending = self._pending.get(trace_id)
            if (
                pending is None
                or mission_id in self._revoked_missions
                or pending.mission_id != mission_id
                or pending.expires_at_ns <= time.time_ns()
            ):
                if pending is not None and pending.expires_at_ns <= time.time_ns():
                    self._pending.pop(trace_id, None)
                return None
            if self._durable_claim is not None:
                try:
                    claimed = await self._durable_claim(
                        trace_id, mission_id, pending.tool_name, decision
                    )
                except Exception:  # noqa: BLE001 - fail closed on DB errors
                    logger.exception("Durable mission tool decision claim failed")
                    return None
                if not claimed:
                    self._pending.pop(trace_id, None)
                    self._mark_resolved(trace_id, time.time_ns())
                    return None
            self._pending.pop(trace_id, None)
            # Close the replay window before awaiting EventBus.publish.
            self._mark_resolved(trace_id, time.time_ns())
            return pending

    async def deny_mission(self, mission_id: str, *, reason: str) -> int:
        """Revoke all outstanding approval tickets for one cancelled mission.

        Remove the tickets under the same lock as `approve`, then publish
        denials to wake any waiting ToolExecutor. Other missions are untouched.
        Already-approved or currently executing tools are outside this gate;
        the orchestrator's cancellation must still stop those workers.
        """
        async with self._lock:
            # Record the cancellation even when no ticket currently exists:
            # a worker can emit ActionApprovalRequired after this revocation.
            self._revoked_missions.add(mission_id)
            pending = tuple(
                item for item in self._pending.values()
                if item.mission_id == mission_id
            )
            for item in pending:
                self._pending.pop(item.trace_id, None)
                self._mark_resolved(item.trace_id, time.time_ns())
        for item in pending:
            await self._bus.publish(
                ActionDenied(
                    trace_id=item.trace_id,
                    tool_name=item.tool_name,
                    reason=reason,
                )
            )
        return len(pending)

    async def deny_all(self, *, reason: str) -> None:
        """Release every waiter during orderly application shutdown."""
        async with self._lock:
            pending = tuple(self._pending.values())
            self._pending.clear()
            for item in pending:
                self._mark_resolved(item.trace_id, time.time_ns())
        for item in pending:
            await self._bus.publish(
                ActionDenied(
                    trace_id=item.trace_id,
                    tool_name=item.tool_name,
                    reason=reason,
                )
            )


class MissionToolAutoApprover:
    """Answers approval gates for tools pre-authorized to one mission (ADR-0031).

    Modeled on ``jarvis.tasks.approval_bridge.TaskAutoApprover``: on a matching
    ``ActionApprovalRequired`` it publishes ``ActionApproved`` onto the bus —
    this ANSWERS the gate, it never bypasses it, so the full
    Proposed → ApprovalRequired → Approved → Executed audit chain is preserved
    and the ``MissionToolApprovalCoordinator``'s pending entry clears through
    its normal ``_on_resolved`` subscription.

    Arming is owned by the ``WorkerToolBroker``: one arm per issued grant
    (keyed ``mission_id`` + a non-secret grant key), disarmed on every
    revocation path, so auto-approval lives exactly as long as a grant. The
    armed set is already triple-filtered upstream (registry ``worker_allowed ∧
    ¬dangerous`` → broker denylist → ``granted ∩ [phase6.safety]
    auto_approve_tool_families``); this class re-checks fail-closed anyway.

    Thread-safety: ``arm``/``disarm`` are called from broker code paths that
    may run off the event loop (reaper via the HTTP thread, ``__del__``
    finalizers), so the grant map is guarded by a ``threading.Lock``. The bus
    handler runs on the loop and only reads under the same lock.
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        coordinator: MissionToolApprovalCoordinator | None = None,
    ) -> None:
        self._bus = bus
        # Share the per-mission decision guard with REST cancel/approve.
        # A grant checked before cancellation cannot publish an approval
        # *after* the cancellation has acquired this guard.
        self._coordinator = coordinator
        self._mutex = threading.Lock()
        # mission_id -> grant_key -> auto-approvable tool names for that grant.
        self._grants: dict[str, dict[str, frozenset[str]]] = {}
        self._revoked_missions: set[str] = set()
        bus.subscribe(ActionApprovalRequired, self._on_approval_required)

    def arm(
        self,
        mission_id: str,
        grant_key: str,
        tool_names: frozenset[str],
    ) -> None:
        """Pre-authorize ``tool_names`` while the grant ``grant_key`` is live.

        An empty set is a valid no-op arm (keeps arm/disarm symmetric for
        grants whose intersection with the allowlist is empty).
        """
        mission = str(mission_id or "").strip()
        if not mission:
            return
        with self._mutex:
            if mission not in self._revoked_missions:
                self._grants.setdefault(mission, {})[grant_key] = frozenset(tool_names)

    def disarm(self, mission_id: str, grant_key: str) -> None:
        mission = str(mission_id or "").strip()
        with self._mutex:
            grants = self._grants.get(mission)
            if grants is None:
                return
            grants.pop(grant_key, None)
            if not grants:
                self._grants.pop(mission, None)

    def revoke_mission(self, mission_id: str) -> None:
        """Disarm all grants and reject later re-arms for a cancelled mission.

        This is an in-process safety barrier; it cannot undo approvals already
        published, or prevent work running in a separate process.
        """
        mission = str(mission_id or "").strip()
        if not mission:
            return
        with self._mutex:
            self._revoked_missions.add(mission)
            self._grants.pop(mission, None)

    async def _on_approval_required(self, event: ActionApprovalRequired) -> None:
        mission_id = str(event.mission_id or "").strip()
        if not mission_id:
            return
        # The same guard covers the REST cancellation's authoritative state
        # transition and revocation. Keep it through bus.publish so a
        # previously read grant cannot emit ActionApproved after cancellation.
        # A standalone approver (without REST coordinator) keeps its legacy
        # behaviour for task-only contexts and unit tests.
        if self._coordinator is None:
            await self._approve_if_authorized(event, mission_id)
        else:
            async with self._coordinator.mission_decision_guard(mission_id):
                await self._approve_if_authorized(event, mission_id)

    async def _approve_if_authorized(
        self, event: ActionApprovalRequired, mission_id: str
    ) -> None:
        # Never a "block" answer; approve both gate reasons — the plausibility
        # guard is a voice-context heuristic without signal for a worker.
        if event.risk_tier not in ("ask", "monitor"):
            return
        if event.reason not in ("risk_tier", "plausibility"):
            return
        # Standalone legacy approvers accept events without an expiry; the
        # mission coordinator requires a live ToolExecutor deadline.
        if self._coordinator is not None and event.expires_at_ns <= time.time_ns():
            return
        # The same persisted-state gate protects automatic approvals after a
        # restart or a cancellation performed by another server process.
        # Under the production coordinator guard this also serializes local
        # cancellation with the check and subsequent approval publication.
        if self._coordinator is not None and not await self._coordinator.mission_is_active(mission_id):
            return
        with self._mutex:
            if mission_id in self._revoked_missions:
                return
            grant_sets = tuple(self._grants.get(mission_id, {}).values())
        granted: frozenset[str] = frozenset().union(*grant_sets) if grant_sets else frozenset()
        if not granted:
            return
        tool_name = event.tool_name
        # Defense in depth: an armed name must still pass the broker denylist.
        from jarvis.missions.workers.worker_tool_broker import worker_tool_name_allowed

        if not worker_tool_name_allowed(tool_name):
            return
        if not self._tool_is_granted(tool_name, granted):
            return
        # A duplicate bus request must not emit a second approval after the
        # first was resolved, nor resurrect a manually denied trace. The
        # claim is atomic with the coordinator's pending/decision projection.
        if self._coordinator is not None:
            if not await self._coordinator.claim_automatic_trace(event):
                return
            # A remote cancellation can commit while the durable trace claim
            # is in flight. Never emit a positive decision when that
            # cancellation is already visible in the authoritative store.
            if not await self._coordinator.mission_is_active(mission_id):
                await self._bus.publish(
                    ActionDenied(
                        trace_id=event.trace_id,
                        tool_name=event.tool_name,
                        reason="mission_inactive_after_claim",
                    )
                )
                return
            with self._mutex:
                revoked = mission_id in self._revoked_missions
            if revoked:
                await self._bus.publish(
                    ActionDenied(
                        trace_id=event.trace_id,
                        tool_name=event.tool_name,
                        reason="mission_cancelled_after_claim",
                    )
                )
                return
        approved_by = f"mission-grant:{mission_id[:13]}"
        logger.info(
            "MissionToolAutoApprover: approving {} for mission {} ({})",
            tool_name,
            mission_id,
            event.reason,
        )
        await self._bus.publish(
            ActionApproved(
                trace_id=event.trace_id,
                tool_name=tool_name,
                approved_by=approved_by,
            )
        )

    @staticmethod
    def _tool_is_granted(tool_name: str, granted: frozenset[str]) -> bool:
        """Exact name, or the MCP ``server/`` prefix (mirror of
        ``TaskAutoApprover._tool_is_granted``)."""
        if tool_name in granted:
            return True
        prefix = tool_name.split("/", 1)[0]
        return prefix in granted


__all__ = [
    "MissionToolApprovalCoordinator",
    "MissionToolAutoApprover",
    "PendingMissionToolApproval",
]

"""ToolExecutor: orchestrates risk-eval → plausibility → approval → execute → event log.

The only authorized entry point for tool calls. Calling `Tool.execute()`
directly bypasses safety — that is a bug.

Phase 4 (persona mandate): a plausibility check runs before every approval
decision. If the voice pipeline has registered a ``plausibility_context_fn``,
the executor fetches transcript confidence + wake age and decides whether
``ask``/``monitor`` tools need an extra confirmation (see
``jarvis.brain.plausibility``).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from jarvis.core.bus import EventBus
from jarvis.core.events import (
    ActionApprovalRequired,
    ActionApproved,
    ActionDenied,
    ActionExecuted,
    ActionProposed,
)
from jarvis.core.protocols import (
    CancelToken,
    ExecutionContext,
    Tool,
    ToolResult,
    Transcript,
)
from jarvis.core.redact import safe_preview

from .approval import TIMEOUT_REASON, ApprovalWorkflow
from .approval_surface import (
    CONVERSATIONAL,
    UNATTENDED,
    resolve_approval_surface,
)
from .origin import OriginLedger, TargetOrigin, classify_targets
from .risk_tier import ActionBlocked, RiskTierEvaluator

if TYPE_CHECKING:
    from jarvis.brain.plausibility import PlausibilityDecision
    from jarvis.core.config import BrainPlausibilityConfig


log = logging.getLogger(__name__)


# Sentinel returned (as ``ToolResult.error``) when a confirmation-requiring tool
# is invoked on a CONVERSATIONAL turn (``config_snapshot["voice_confirm"]``).
# Instead of blocking in ``ApprovalWorkflow.wait()`` for a UI approval no voice/
# chat user can give (which is then beheaded by the 20 s no-first-frame ceiling →
# the misleading "took too long" phrase, forensic 2026-06-18), the executor stashes
# the action and returns this sentinel so the brain SPEAKS a confirmation question
# and ends the turn. The next "ja" re-runs the action via ``execute_confirmed``.
VOICE_CONFIRM_SENTINEL = "__voice_confirm_required__"


# The three ways an approval-gated call can end WITHOUT running. They carry
# distinct ``ToolResult.error`` prefixes on purpose: until the GT-12 fix a
# 60-second timeout came back as ``approval-denied (timeout)``, so every
# consumer that classifies on ``startswith("approval-denied")`` — the mission
# ``WorkerToolBroker`` does exactly that — recorded "the user refused" for a
# decision nobody was ever asked to make. A refusal, a silence, and an absent
# human are three different facts and must read as three different facts.
APPROVAL_DENIED_PREFIX = "approval-denied"
APPROVAL_TIMEOUT_PREFIX = "approval-timeout"
APPROVAL_UNAVAILABLE_PREFIX = "approval-unavailable"

#: ``ToolResult.output["outcome"]`` for the unattended dead end, and the
#: ``ActionDenied.reason`` published alongside it. The word says what happened:
#: approval was impossible, not withheld.
APPROVAL_UNAVAILABLE_OUTCOME = "approval_unavailable"


# ``plausibility_context_fn`` returns (Transcript | None, wake_age_s | None).
# The voice pipeline registers a provider that supplies the last user-turn
# transcript and the seconds since the last wake trigger. On
# ``None`` returns the executor behaves as before (no plausibility check).
PlausibilityContextFn = Callable[[], "tuple[Transcript | None, float | None]"]


def _optional_string(value: Any) -> str | None:
    """Normalize optional correlation metadata without inventing identifiers."""
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


class ToolExecutor:
    """Pipeline: evaluate → (plausibility) → (approve) → execute → log."""

    def __init__(
        self,
        bus: EventBus,
        evaluator: RiskTierEvaluator,
        approval: ApprovalWorkflow,
        *,
        default_timeout_s: float = 60.0,
        plausibility_config: BrainPlausibilityConfig | None = None,
        plausibility_context_fn: PlausibilityContextFn | None = None,
    ) -> None:
        self._bus = bus
        self._evaluator = evaluator
        self._approval = approval
        self._default_timeout_s = default_timeout_s
        self._plausibility_config = plausibility_config
        self._plausibility_context_fn = plausibility_context_fn
        # Two-turn voice/chat confirmation: actions deferred by ``execute`` on a
        # conversational turn, keyed by trace_id, awaiting an ``execute_confirmed``
        # (user said "ja") or ``cancel_pending`` (user said "nein"). The tool +
        # args live here OUT-OF-BAND — never in the serialized ToolResult.output.
        self._pending_voice: dict[UUID, tuple[Tool, dict[str, Any]]] = {}
        # ADR-0040, record only: the addressable values tool results held, per
        # trace, so a call's target can be told apart by where it came from.
        # Kept here because this is the one entry point every tool call takes.
        self._origins = OriginLedger()
        # Strong references to still-running tools that ignored cancellation.
        # Their one-shot execution leases stay "running" until they really end.
        self._unresponsive_mission_tools: set[asyncio.Task[Any]] = set()

    #: The longest a surface may keep an approval open, whatever it asks for.
    MAX_APPROVAL_TIMEOUT_S = 900.0
    # A coroutine may suppress CancelledError. Never let one uncooperative
    # tool hold the mission API open indefinitely while its lease is pending.
    MISSION_CANCEL_GRACE_S = 0.75

    def _approval_timeout(self, config_snapshot: dict[str, Any] | None) -> float:
        """The wait for this call: the surface's ``approval_timeout_s`` when it
        declares one (never below the default, never above the cap), else the
        default the executor was built with."""
        raw = (config_snapshot or {}).get("approval_timeout_s")
        if raw is None:
            return self._default_timeout_s
        try:
            asked = float(raw)
        except (TypeError, ValueError):
            return self._default_timeout_s
        return max(self._default_timeout_s, min(asked, self.MAX_APPROVAL_TIMEOUT_S))

    def set_plausibility_context_fn(
        self, fn: PlausibilityContextFn | None,
    ) -> None:
        """Late registration of the plausibility-context provider.

        The voice pipeline calls this after its own ``run()`` setup, because
        the ToolExecutor is built earlier in the bootstrap order than
        the pipeline. Idempotent — ``None`` resets the hook.
        """
        self._plausibility_context_fn = fn

    def _evaluate_plausibility(
        self,
        tool: Tool,
        decision: Any,
    ) -> PlausibilityDecision | None:
        """Fetches the current plausibility context and checks it.

        Returns ``None`` if no context provider is registered, or the
        tool was downgraded to ``safe`` via whitelist — whitelist logic
        is sacred (mandate: "whitelist-downgraded tools keep running
        without a plausibility check").
        """
        if self._plausibility_context_fn is None:
            return None
        # Whitelist downgrade: skip plausibility.
        if decision.approved_by == "whitelist":
            return None
        try:
            transcript, wake_age = self._plausibility_context_fn()
        except Exception as exc:  # noqa: BLE001
            log.debug("plausibility_context_fn failed: %s", exc)
            return None
        from jarvis.brain.plausibility import check_plausibility

        return check_plausibility(
            tool_name=tool.name,
            risk_tier=decision.tier,
            transcript=transcript,
            wake_age_s=wake_age,
            config=self._plausibility_config,
        )

    def _target_origin(
        self, tool: Tool, args: dict[str, Any], tid: UUID, user_utterance: str
    ) -> TargetOrigin:
        """Where this call's target came from. Never decides anything and never
        raises: a classification that fails is an empty record, not a failed
        call."""
        try:
            return classify_targets(
                tool, args, ledger=self._origins, trace=str(tid), user_text=user_utterance
            )
        except Exception as exc:  # noqa: BLE001 — a record must never cost the call
            log.debug("target-origin classification failed for %s: %s", tool.name, exc)
            return TargetOrigin()

    def _record_result_origin(
        self, tool: Tool, tid: UUID, result: ToolResult, args: dict[str, Any]
    ) -> None:
        """Remember the addressable values a tool result held (best effort)."""
        try:
            self._origins.record(str(tid), tool, result.output, args)
        except Exception as exc:  # noqa: BLE001 — a record must never cost the call
            log.debug("origin ledger could not read the result of %s: %s", tool.name, exc)

    async def publish_guard_denied(
        self,
        tool_name: str,
        reason: str,
        *,
        trace_id: UUID | None = None,
    ) -> None:
        """Surface a tool call refused BEFORE reaching :meth:`execute`.

        The tool-use loop's deterministic guards (how-to question, research
        intent, STT hallucination, unknown tool name, …) block a call without
        ever entering the executor — so no ActionProposed/ActionExecuted event
        fires and the session timeline shows NO trace of why a turn refused
        (2026-07-06 audit: the unknown-tool 'run-shell' incident left zero
        events). Publishing the same ``ActionDenied`` the blacklist path uses
        makes the refusal visible; the recorder already subscribes to it.
        Best-effort: observability must never break tool execution.
        """
        try:
            await self._bus.publish(ActionDenied(
                trace_id=trace_id or uuid4(),
                tool_name=tool_name,
                reason=reason,
            ))
        except Exception:  # noqa: BLE001
            log.debug("publish_guard_denied failed", exc_info=True)

    async def _approval_unavailable(
        self,
        tool: Tool,
        tid: UUID,
        risk_tier: str,
        config_snapshot: dict[str, Any] | None,
    ) -> ToolResult:
        """The honest dead end: this action needed a human and had none.

        Publishes ``ActionDenied`` so the refusal is visible in the timeline
        (same reasoning as :meth:`publish_guard_denied` — a call that silently
        vanishes is worse than one that failed), but says in the reason WHY it
        did not run. The returned error carries its own prefix so no consumer
        can mistake it for a user's "no", and the output carries a
        plain-language sentence a person can actually read in a run log.
        """
        reason = f"{APPROVAL_UNAVAILABLE_OUTCOME}: no approval channel on this surface"
        await self._bus.publish(ActionDenied(
            trace_id=tid,
            tool_name=tool.name,
            reason=reason,
        ))
        log.info(
            "approval-unavailable: %s (tier=%s) needed a confirmation and this "
            "surface has nobody to give it — failing fast",
            tool.name, risk_tier,
        )
        output: dict[str, Any] = {
            "outcome": APPROVAL_UNAVAILABLE_OUTCOME,
            "tool_name": tool.name,
            "trace_id": str(tid),
            "risk_tier": risk_tier,
        }
        # Plain-language sentence in the turn's ALREADY-RESOLVED output
        # language (Runtime Output Language doctrine: this layer reads the
        # resolved value, it never re-derives one). Phrasing lives in the one
        # channel-agnostic table, next to the confirmation question it belongs
        # to. Lazy import — ``jarvis.voice`` couples to ``jarvis.core.self_mod``
        # via its package __init__, so importing at module load would create an
        # order-dependent cycle (same pattern as the tool-use loop).
        try:
            from jarvis.voice.tool_confirmation import format_approval_unavailable

            output["message"] = format_approval_unavailable(
                tool.name,
                language=str((config_snapshot or {}).get("output_language") or ""),
            )
        except Exception as exc:  # noqa: BLE001 — phrasing must not decide safety
            log.debug("approval-unavailable phrasing failed: %s", exc)
        return ToolResult(
            success=False,
            output=output,
            error=f"{APPROVAL_UNAVAILABLE_PREFIX} ({reason})",
        )

    async def execute(
        self,
        tool: Tool,
        args: dict[str, Any],
        *,
        user_utterance: str = "",
        config_snapshot: dict[str, Any] | None = None,
        memory_read: Any | None = None,
        trace_id: UUID | None = None,
        rationale: str = "",
        cancel_token: CancelToken | None = None,
    ) -> ToolResult:
        tid = trace_id or uuid4()
        t_start = time.perf_counter()

        if (config_snapshot or {}).get("chat_read_only"):
            from jarvis.core.tool_read_only import allows_read

            if not allows_read(tool, args):
                await self._bus.publish(ActionDenied(
                    trace_id=tid, tool_name=tool.name, reason="Plan mode permits reads only",
                ))
                return ToolResult(False, None, "Plan mode permits reads only")

        if cancel_token is not None and cancel_token.is_cancelled():
            return ToolResult(
                success=False,
                output=None,
                error=f"cancelled ({cancel_token.reason or 'requested'})",
            )

        # 1. Evaluate
        try:
            decision = self._evaluator.evaluate(tool, args)
        except ActionBlocked as exc:
            await self._bus.publish(ActionDenied(
                trace_id=tid,
                tool_name=tool.name,
                reason=f"blacklist: {exc.pattern}",
            ))
            return ToolResult(success=False, output=None, error=str(exc))

        # Where the call's target came from (ADR-0040, record only). Settled
        # here, before an approval ticket exists, so nothing but the publish
        # sits between arming it and ``ActionProposed``.
        origin = self._target_origin(tool, args, tid, user_utterance)

        # 2. Plausibility check (Phase 4): the result can force confirmation
        # even when the tier workflow does not (for example, ``monitor``).
        plaus = self._evaluate_plausibility(tool, decision)
        if plaus is not None and plaus.reason != "ok":
            log.info(
                "Plausibility[%s]: tier=%s reason=%s require_confirm=%s",
                tool.name, decision.tier, plaus.reason, plaus.require_confirmation,
            )

        # 3. Arm approval BEFORE publishing ActionProposed. Subscribers such as
        # TaskAutoApprover may answer synchronously from that event; registering
        # afterward loses the answer and turns a valid grant into a timeout.
        approved_by = decision.approved_by or "auto"
        tier_confirm = self._evaluator.needs_user_confirmation(decision)
        plaus_confirm = plaus is not None and plaus.require_confirmation
        # Explicit intent (Claude-Code permission model, mandate 2026-08-08):
        # a consequential action the user's own utterance already asked for by
        # name ("delete the folder X") skips the redundant confirmation. Only
        # the TIER requirement may be waived — a plausibility-forced
        # confirmation (low STT confidence, stale wake) stays binding, because
        # the whole doubt there is whether the utterance was heard right.
        if tier_confirm and not plaus_confirm and user_utterance:
            confirms = getattr(tool, "intent_confirms_args", None)
            if callable(confirms):
                try:
                    if confirms(args, user_utterance):
                        tier_confirm = False
                        approved_by = "explicit-intent"
                        log.info(
                            "explicit-intent: %s authorized by the utterance, "
                            "skipping confirmation", tool.name,
                        )
                except Exception as exc:  # noqa: BLE001 — keep the confirmation on a broken hook
                    log.warning(
                        "intent_confirms_args on %r raised %r — keeping confirmation",
                        tool.name, exc,
                    )
        needs_confirm = tier_confirm or plaus_confirm
        # A bound chat owns its approval stance through ChatApprovalBridge.
        # Calls from voice, tasks, missions and other app surfaces use the
        # global default. This never overrides the block/read-only/cancel
        # checks, and model-controlled tool arguments cannot set the policy.
        bypass_default = self._evaluator.bypass_permissions and not (
            config_snapshot or {}
        ).get("approval_ref")
        # WHO can answer this gate — declared by the calling layer, never by
        # the model (see ``approval_surface``). It changes only how the
        # approval is obtained, never whether one is needed: ``needs_confirm``
        # above is the untouched tier decision.
        surface = resolve_approval_surface(config_snapshot)
        voice_confirm = surface == CONVERSATIONAL and not bypass_default
        # How long a card may stay open — declared by the calling layer like
        # the surface itself (a person reading a chat card must not lose the
        # tool to a clock built for a spoken "ja"), clamped so no surface can
        # park the executor forever.
        approval_timeout_s = self._approval_timeout(config_snapshot)
        approval_ticket = None
        if needs_confirm and not voice_confirm:
            # An unattended call arms too: the pre-authorization bridges answer
            # synchronously on the ActionApprovalRequired publish below, and
            # ADR-0031 relies on the ticket existing before that publish.
            approval_ticket = self._approval.arm(tid)

        # 3.5 Proposed event (the UI can use this as a live indicator). The
        # rationale is redacted and capped so no raw secret reaches the bus.
        try:
            await self._bus.publish(ActionProposed(
                trace_id=tid,
                tool_name=tool.name,
                args=args,
                risk_tier=decision.tier,
                rationale=safe_preview(rationale),
                effect=origin.effect,
                target_origin=origin.origin,
                origin_source=origin.source,
            ))
        except BaseException:
            if approval_ticket is not None:
                approval_ticket.close()
            raise

        if approval_ticket is not None:
            reason = (
                "plausibility"
                if plaus is not None and plaus.require_confirmation
                else "risk_tier"
            )
            # An unattended call expires immediately: it will not be waited on,
            # so advertising a 60-second window would invite a UI to offer a
            # decision that can no longer reach anybody.
            window_ns = (
                0
                if surface == UNATTENDED
                else int(approval_timeout_s * 1_000_000_000)
            )
            await self._bus.publish(
                ActionApprovalRequired(
                    trace_id=tid,
                    tool_name=tool.name,
                    risk_tier=decision.tier,
                    reason=reason,
                    args_preview=safe_preview(args),
                    expires_at_ns=time.time_ns() + window_ns,
                    mission_id=_optional_string(
                        (config_snapshot or {}).get("mission_id")
                    ),
                    worker_id=_optional_string(
                        (config_snapshot or {}).get("worker_id")
                    ),
                    approval_ref=_optional_string(
                        (config_snapshot or {}).get("approval_ref")
                    ),
                    effect=origin.effect,
                    target_origin=origin.origin,
                    origin_source=origin.source,
                )
            )
            # Scoped authorizers have the first decision, including explicit
            # denials. Bypass supplies an immediate standing approval only
            # when none has answered; it never overwrites their decision.
            if bypass_default and approval_ticket.peek() is None:
                await self._bus.publish(
                    ActionApproved(
                        trace_id=tid,
                        tool_name=tool.name,
                        approved_by="bypass",
                    )
                )

        if needs_confirm:
            # Two-turn confirmation on a conversational turn: do NOT block on the
            # UI-approval future (no voice/chat user can resolve it within the
            # turn's latency window). Stash the action and return the sentinel so
            # the brain speaks a confirmation question; the user's next "ja" calls
            # ``execute_confirmed`` (AD-OE: the talker never awaits heavy/blocking
            # work on the turn). ``needs_confirm`` already excludes whitelist
            # downgrades, so this fires only for genuinely consequential tools.
            if voice_confirm:
                self._pending_voice[tid] = (tool, dict(args))
                log.info(
                    "voice-confirm: deferring %s (tier=%s) for two-turn confirmation",
                    tool.name, decision.tier,
                )
                sentinel_output: dict[str, Any] = {
                    "tool_name": tool.name,
                    "trace_id": str(tid),
                    "risk_tier": decision.tier,
                }
                # Optional hook (like ``risk_tier_for_args``): a tool may
                # summarize what the deferred action would DO so the spoken
                # confirmation question can say it in plain language.
                # Best-effort — phrasing must never block the confirm flow.
                describe = getattr(tool, "describe_args", None)
                if callable(describe):
                    try:
                        summary = describe(args)
                    except Exception as exc:  # noqa: BLE001
                        log.debug("describe_args on %r failed: %s", tool.name, exc)
                        summary = None
                    if isinstance(summary, dict) and summary:
                        sentinel_output["impact"] = {
                            str(k): str(v) for k, v in summary.items()
                        }
                return ToolResult(
                    success=False,
                    output=sentinel_output,
                    error=VOICE_CONFIRM_SENTINEL,
                )
            if surface == UNATTENDED:
                # Nobody is there to ask. The pre-authorization bridges have
                # already had their say — they answer inline while the
                # ActionApprovalRequired publish above is awaited — so a ticket
                # that is still undecided will stay undecided forever. Waiting
                # the full timeout would buy a minute of silence and then
                # report a refusal nobody made (audit GT-12).
                decided = (
                    approval_ticket.peek() if approval_ticket is not None else None
                )
                if decided is None:
                    if approval_ticket is not None:
                        approval_ticket.close()
                    return await self._approval_unavailable(
                        tool, tid, decision.tier, config_snapshot,
                    )
                approved, who_or_reason = decided
                if approval_ticket is not None:
                    approval_ticket.close()
            else:
                try:
                    approved, who_or_reason = await self._approval.wait(
                        tid, approval_timeout_s
                    )
                finally:
                    if approval_ticket is not None:
                        approval_ticket.close()
            if not approved:
                await self._bus.publish(ActionDenied(
                    trace_id=tid,
                    tool_name=tool.name,
                    reason=who_or_reason,
                ))
                # A silence is not a "no". Keep the two apart so the caller,
                # the mission broker, and the run log can each say which
                # actually happened.
                if who_or_reason == TIMEOUT_REASON:
                    error = (
                        f"{APPROVAL_TIMEOUT_PREFIX} (nobody decided within "
                        f"{approval_timeout_s:.0f}s)"
                    )
                else:
                    error = f"{APPROVAL_DENIED_PREFIX} ({who_or_reason})"
                return ToolResult(success=False, output=None, error=error)
            approved_by = who_or_reason  # "user" or "auto"

        if cancel_token is not None and cancel_token.is_cancelled():
            await self._bus.publish(ActionDenied(
                trace_id=tid,
                tool_name=tool.name,
                reason=f"cancelled: {cancel_token.reason or 'requested'}",
            ))
            return ToolResult(
                success=False,
                output=None,
                error=f"cancelled ({cancel_token.reason or 'requested'})",
            )

        # Final mission lifecycle gate, after approval and immediately before
        # invoking the tool. A cancellation in another server process can
        # arrive while a human approval card is open; the earlier approval
        # checks alone cannot stop that tool from running. Only supervisor-
        # stamped mission-worker calls are subject to this extra check; other
        # voice/chat/task surfaces keep their existing behavior. This narrows
        # the distributed check-to-use window but is NOT an atomic lease with
        # the mission cancellation transaction.
        if (config_snapshot or {}).get("tool_origin") == "mission_worker":
            from jarvis.core import runtime_refs

            mission_id = _optional_string((config_snapshot or {}).get("mission_id"))
            checker = runtime_refs.get_mission_tool_execution_checker()
            active = False
            if mission_id and callable(checker):
                try:
                    active = bool(await checker(mission_id))
                except Exception:  # noqa: BLE001 - database outage denies execution
                    log.exception("Mission execution lifecycle check failed")
            if not active:
                await self._bus.publish(ActionDenied(
                    trace_id=tid,
                    tool_name=tool.name,
                    reason="mission_inactive_before_execution",
                ))
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"{APPROVAL_DENIED_PREFIX} (mission_inactive_before_execution)",
                )

        # V25: durable one-shot execution claim. This is a SQLite write barrier
        # against duplicate starts and cancellations *already committed* in
        # another process. The legacy standalone path (no registered claimer)
        # retains the V24 read gate; the production WebServer registers both.
        mission_execution_checker = None
        mission_execution_finisher = None
        mission_execution_activator = None
        mission_execution_lease_checker = None
        mission_execution_dispatch_fence = None
        claimed_execution = False
        if (config_snapshot or {}).get("tool_origin") == "mission_worker":
            from jarvis.core import runtime_refs

            mission_execution_checker = runtime_refs.get_mission_tool_execution_checker()
            claimer = runtime_refs.get_mission_tool_execution_claimer()
            mission_execution_finisher = runtime_refs.get_mission_tool_execution_finisher()
            mission_execution_activator = runtime_refs.get_mission_tool_execution_activator()
            mission_execution_lease_checker = (
                runtime_refs.get_mission_tool_execution_lease_checker()
            )
            mission_execution_dispatch_fence = (
                runtime_refs.get_mission_tool_execution_dispatch_fence()
            )
            if claimer is not None:
                try:
                    claimed_execution = bool(await claimer(tid, mission_id, tool.name))
                except Exception:  # noqa: BLE001
                    log.exception("Mission execution lease claim failed")
                if not claimed_execution or not callable(mission_execution_finisher):
                    await self._bus.publish(ActionDenied(
                        trace_id=tid, tool_name=tool.name,
                        reason="mission_execution_claim_refused",
                    ))
                    return ToolResult(
                        success=False, output=None,
                        error=f"{APPROVAL_DENIED_PREFIX} (mission_execution_claim_refused)",
                    )

        # 4. Execute
        ctx = ExecutionContext(
            trace_id=tid,
            user_utterance=user_utterance,
            config=config_snapshot or {},
            memory_read=memory_read,
            approved_by=approved_by,
        )
        execution_outcome = "failed"
        deferred_execution_finish = False

        def track_unresponsive(work: asyncio.Task[Any]) -> None:
            nonlocal deferred_execution_finish
            deferred_execution_finish = True
            self._track_unresponsive_mission_tool(
                work, trace_id=tid, finisher=mission_execution_finisher,
                tool_name=tool.name, tool=tool,
            )

        try:
            if claimed_execution:
                # V26: the claim reserves the trace; activation is a separate
                # SQLite writer fence. A cancellation committed between those
                # two steps must prevent the external tool from starting.
                # Legacy injected claimers without an activator retain a
                # second lifecycle read for backwards compatibility.
                active_at_start = False
                try:
                    if callable(mission_execution_activator):
                        active_at_start = bool(await mission_execution_activator(
                            tid, mission_id, tool.name,
                        ))
                    elif callable(mission_execution_checker):
                        active_at_start = bool(await mission_execution_checker(mission_id))
                except Exception:  # noqa: BLE001
                    log.exception("Mission execution activation check failed")
                if not active_at_start:
                    execution_outcome = "cancelled"
                    await self._bus.publish(ActionDenied(
                        trace_id=tid, tool_name=tool.name,
                        reason="mission_inactive_after_execution_claim",
                    ))
                    return ToolResult(
                        success=False, output=None,
                        error=f"{APPROVAL_DENIED_PREFIX} (mission_inactive_after_execution_claim)",
                    )
            if claimed_execution and callable(mission_execution_checker):
                result = await self._execute_with_mission_cancel_watch(
                    tool, args, ctx, mission_id, mission_execution_checker,
                    cancel_token=cancel_token,
                    on_unresponsive=track_unresponsive,
                    lease_checker=mission_execution_lease_checker,
                    dispatch_fence=mission_execution_dispatch_fence,
                )
            else:
                result = await tool.execute(args, ctx)
            execution_outcome = (
                "cancelled"
                if result.error == f"{APPROVAL_DENIED_PREFIX} (mission_cancelled_during_execution)"
                else "completed" if result.success else "failed"
            )
            if execution_outcome == "cancelled":
                await self._bus.publish(ActionDenied(
                    trace_id=tid, tool_name=tool.name,
                    reason="mission_cancelled_during_execution",
                ))
        except asyncio.CancelledError:
            execution_outcome = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001
            duration_ms = int((time.perf_counter() - t_start) * 1000)
            await self._bus.publish(ActionExecuted(
                trace_id=tid,
                tool_name=tool.name,
                success=False,
                duration_ms=duration_ms,
                error=str(exc),
            ))
            return ToolResult(success=False, output=None, error=str(exc))
        finally:
            if claimed_execution and not deferred_execution_finish:
                try:
                    committed_outcome = await mission_execution_finisher(
                        tid, execution_outcome,
                    )
                    # V41: SQLite serializes this final writer with any
                    # concurrent pause/cancel.  A committed cancel latch can
                    # win AFTER the post-result read; never report success
                    # when the durable lease actually says cancelled.
                    if execution_outcome == "completed" and committed_outcome == "cancelled":
                        result = ToolResult(
                            success=False, output=None,
                            error=f"{APPROVAL_DENIED_PREFIX} (mission_cancelled_during_execution)",
                        )
                        await self._bus.publish(ActionDenied(
                            trace_id=tid, tool_name=tool.name,
                            reason="mission_cancelled_during_execution",
                        ))
                    elif committed_outcome is not None and committed_outcome != execution_outcome:
                        raise RuntimeError("unexpected committed execution outcome")
                except Exception:  # noqa: BLE001
                    # A failed final write cannot prove success.  The durable
                    # one-shot reservation remains non-replayable, even if
                    # its outcome is unknown or the database is unavailable.
                    log.exception("Mission execution lease finalisation failed")
                    if execution_outcome == "completed":
                        result = ToolResult(
                            success=False, output=None,
                            error=f"{APPROVAL_DENIED_PREFIX} (mission_execution_finalization_failed)",
                        )

        self._record_result_origin(tool, tid, result, args)
        duration_ms = int((time.perf_counter() - t_start) * 1000)
        await self._bus.publish(ActionExecuted(
            trace_id=tid,
            tool_name=tool.name,
            success=result.success,
            duration_ms=duration_ms,
            error=result.error,
            output_preview=safe_preview(result.output),
        ))
        return result

    def _track_unresponsive_mission_tool(
        self, work: asyncio.Task[Any], *, trace_id: UUID, finisher: Any,
        tool_name: str, tool: Tool | None = None,
    ) -> None:
        """Keep a cancelled-but-running task visible until it actually stops.

        We deliberately leave its durable one-shot lease in ``running`` while
        it might still perform side effects. Never reclaim or retry that trace.
        When it eventually finishes, mark the cancellation and consume any
        late exception to avoid an unobserved-task warning.
        """
        self._unresponsive_mission_tools.add(work)
        log.error(
            "Mission tool %s trace=%s ignored cancellation after %.2fs; "
            "execution may still have side effects; lease remains running",
            tool_name, trace_id, self.MISSION_CANCEL_GRACE_S,
        )

        def completed(task: asyncio.Task[Any]) -> None:
            self._unresponsive_mission_tools.discard(task)
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except BaseException:  # noqa: BLE001 - consume late tool errors
                log.exception(
                    "Unresponsive mission tool %s trace=%s ended with error",
                    tool_name, trace_id,
                )
            else:
                log.error(
                    "Mission tool %s trace=%s completed AFTER cancellation; "
                    "external side effects cannot be rolled back",
                    tool_name, trace_id,
                )

            async def finish() -> None:
                # The Python coroutine can be cancelled a second time while
                # run_shell's strongly referenced OS cleanup is still active.
                # The lease must not say "finished" until that cleanup ends.
                pending = getattr(tool, "pending_cleanup_for_task", None)
                if callable(pending):
                    try:
                        cleanup = pending(task)
                        if cleanup is not None:
                            await asyncio.shield(cleanup)
                    except BaseException:
                        # A failed/cancelled OS cleanup may have left the
                        # process alive. Never record a terminal lease: a
                        # crash/retry must not launch this trace a second time.
                        log.exception(
                            "Mission tool %s trace=%s subprocess cleanup failed; "
                            "execution lease remains running",
                            tool_name, trace_id,
                        )
                        return
                cleanup_failed = getattr(tool, "cleanup_failed_for_task", None)
                if callable(cleanup_failed):
                    try:
                        if cleanup_failed(task):
                            log.error(
                                "Mission tool %s trace=%s OS cleanup was not "
                                "confirmed; execution lease remains running",
                                tool_name, trace_id,
                            )
                            return
                    except BaseException:
                        log.exception(
                            "Mission tool %s trace=%s OS cleanup status "
                            "unavailable; execution lease remains running",
                            tool_name, trace_id,
                        )
                        return
                try:
                    await finisher(trace_id, "cancelled")
                except Exception:  # noqa: BLE001 - keep one-shot claim
                    log.exception(
                        "Deferred mission execution lease finalisation failed: %s",
                        trace_id,
                    )

            # This is the same event loop that owned the tool. If the loop is
            # shutting down, its durable claim remains reserved for safety.
            loop = task.get_loop()
            if loop.is_closed():
                log.error("Event loop closed before lease finalisation: %s", trace_id)
            else:
                loop.create_task(finish())

        work.add_done_callback(completed)

    async def _execute_with_mission_cancel_watch(
        self, tool: Tool, args: dict[str, Any], ctx: ExecutionContext,
        mission_id: str, checker: Any, *,
        cancel_token: CancelToken | None = None,
        on_unresponsive: Callable[[asyncio.Task[Any]], None] | None = None,
        lease_checker: Any | None = None,
        dispatch_fence: Any | None = None,
    ) -> ToolResult:
        """Watch lifecycle AND the durable per-trace cancel latch (when wired).

        A non-cooperative coroutine may ignore ``Task.cancel()``. In that case
        the request returns after a bounded grace period, while its durable
        execution claim stays reserved until the tool really finishes. This
        cannot kill blocking Python code or undo external side effects.
        """
        async def permitted() -> bool:
            if not await checker(mission_id):
                return False
            # The header/eventlog can be RUNNING again after a brief pause.
            # The per-trace SQLite latch must still terminate this execution.
            if callable(lease_checker):
                return bool(await lease_checker(ctx.trace_id, mission_id, tool.name))
            # Legacy injected checkers without a durable lease callback retain
            # the V28 lifecycle behaviour; the production server wires both.
            return True

        async def watch() -> None:
            while True:
                await asyncio.sleep(0.25)
                if cancel_token is not None and cancel_token.is_cancelled():
                    return
                try:
                    if not await permitted():
                        return
                except Exception:  # noqa: BLE001 - fail closed on DB outage
                    log.exception("Mission execution watch persisted guard failed")
                    return

        if cancel_token is not None and cancel_token.is_cancelled():
            return ToolResult(
                success=False, output=None,
                error=f"{APPROVAL_DENIED_PREFIX} (mission_cancelled_during_execution)",
            )
        try:
            if not await permitted():
                return ToolResult(
                    success=False, output=None,
                    error=f"{APPROVAL_DENIED_PREFIX} (mission_cancelled_during_execution)",
                )
        except Exception:  # noqa: BLE001
            log.exception("Mission execution preflight persisted guard failed")
            return ToolResult(
                success=False, output=None,
                error=f"{APPROVAL_DENIED_PREFIX} (mission_cancelled_during_execution)",
            )

        # V42: serialize this final dispatch decision with an in-flight
        # cancellation SQLite writer. A read preflight may observe the old
        # committed snapshot while the cancellation transaction is pending.
        # Legacy injected checkers have no writer hook and retain the read
        # preflight above. This does not lock external side effects afterwards.
        if callable(dispatch_fence):
            try:
                if not await dispatch_fence(ctx.trace_id, mission_id, tool.name):
                    return ToolResult(
                        success=False, output=None,
                        error=f"{APPROVAL_DENIED_PREFIX} (mission_cancelled_during_execution)",
                    )
            except Exception:  # noqa: BLE001 -- unavailable DB denies dispatch
                log.exception("Mission execution dispatch writer fence failed")
                return ToolResult(
                    success=False, output=None,
                    error=f"{APPROVAL_DENIED_PREFIX} (mission_cancelled_during_execution)",
                )
            if cancel_token is not None and cancel_token.is_cancelled():
                return ToolResult(
                    success=False, output=None,
                    error=f"{APPROVAL_DENIED_PREFIX} (mission_cancelled_during_execution)",
                )

        # V43: the V42 writer fence above runs *before* scheduling the work.
        # Another process can commit cancellation between that statement and
        # the new task's first event-loop turn. Recheck from INSIDE the work
        # task, before entering tool.execute(), so a cancellation in this
        # scheduling gap cannot start an external side effect. This second
        # fence still does not lock cancellation against effects that begin
        # after it returns; it is not a cross-process atomic start protocol.
        async def execute_after_task_start_fence() -> ToolResult:
            if callable(dispatch_fence):
                try:
                    if not await dispatch_fence(ctx.trace_id, mission_id, tool.name):
                        return ToolResult(
                            success=False, output=None,
                            error=f"{APPROVAL_DENIED_PREFIX} (mission_cancelled_during_execution)",
                        )
                except Exception:  # noqa: BLE001 - unavailable DB denies execution
                    log.exception("Mission execution in-task dispatch fence failed")
                    return ToolResult(
                        success=False, output=None,
                        error=f"{APPROVAL_DENIED_PREFIX} (mission_cancelled_during_execution)",
                    )
                if cancel_token is not None and cancel_token.is_cancelled():
                    return ToolResult(
                        success=False, output=None,
                        error=f"{APPROVAL_DENIED_PREFIX} (mission_cancelled_during_execution)",
                    )
            return await tool.execute(args, ctx)

        work = asyncio.create_task(execute_after_task_start_fence())
        watcher = asyncio.create_task(watch())
        try:
            done, _ = await asyncio.wait(
                {work, watcher}, return_when=asyncio.FIRST_COMPLETED,
            )
            # V39: a cancellation and a tool result can become ready in the
            # same event-loop turn.  Never report a successful execution when
            # the durable cancellation watcher also completed.  The tool may
            # already have performed side effects; this is conservative audit
            # classification, NOT rollback or an atomic cancel/start fence.
            if watcher in done:
                return ToolResult(
                    success=False, output=None,
                    error=f"{APPROVAL_DENIED_PREFIX} (mission_cancelled_during_execution)",
                )
            result = await work
            # V40: the 250 ms watcher can still be sleeping when a fast tool
            # finishes after another process commits a pause/cancel.  Recheck
            # the durable per-trace latch before reporting a *successful*
            # result.  This is an audit/result fence, not a rollback or an
            # atomic transaction with external tool side effects.
            if result.success:
                if cancel_token is not None and cancel_token.is_cancelled():
                    return ToolResult(
                        success=False, output=None,
                        error=f"{APPROVAL_DENIED_PREFIX} (mission_cancelled_during_execution)",
                    )
                try:
                    still_permitted = await permitted()
                except Exception:  # noqa: BLE001 - fail closed on DB outage
                    log.exception("Mission execution post-result persisted guard failed")
                    still_permitted = False
                if not still_permitted:
                    return ToolResult(
                        success=False, output=None,
                        error=f"{APPROVAL_DENIED_PREFIX} (mission_cancelled_during_execution)",
                    )
            return result
        finally:
            watcher.cancel()
            if not work.done():
                work.cancel()
                # asyncio.gather()/wait_for() can wait indefinitely when a
                # tool suppresses cancellation. asyncio.wait has a hard grace.
                done, _ = await asyncio.wait(
                    {work}, timeout=self.MISSION_CANCEL_GRACE_S,
                )
                if work not in done and on_unresponsive is not None:
                    on_unresponsive(work)
                elif work not in done:
                    log.error("Unresponsive mission tool without lease finisher")
            if work.done():
                await asyncio.gather(work, return_exceptions=True)
                # A second cancellation can finish the Python tool task
                # while its shell process-tree cleanup is still running.
                # Defer the durable lease finish even though work.done() is
                # already true. The callback waits for the OS cleanup task.
                pending = getattr(tool, "pending_cleanup_for_task", None)
                if callable(pending) and on_unresponsive is not None:
                    cleanup = pending(work)
                    failed = getattr(tool, "cleanup_failed_for_task", None)
                    try:
                        cleanup_failed = callable(failed) and failed(work)
                    except BaseException:
                        # An unreliable status hook is treated as failure.
                        cleanup_failed = True
                        log.exception("Mission shell cleanup status check failed")
                    if cleanup is not None or cleanup_failed:
                        on_unresponsive(work)
            await asyncio.gather(watcher, return_exceptions=True)

    # ------------------------------------------------------------------
    # Two-turn voice/chat confirmation resume (turn N+1)
    # ------------------------------------------------------------------

    def has_pending_voice_confirm(self, trace_id: UUID) -> bool:
        """True while an action deferred for ``trace_id`` still awaits a yes/no."""
        return trace_id in self._pending_voice

    async def execute_confirmed(
        self,
        trace_id: UUID,
        *,
        user_utterance: str = "",
        config_snapshot: dict[str, Any] | None = None,
        memory_read: Any | None = None,
    ) -> ToolResult:
        """Run the action stashed by a prior voice-confirm deferral ("ja").

        Single-use: the pending entry is popped first, so a repeated "ja" cannot
        double-fire the side effect. ``approved_by="user"`` records that the human
        authorized it. Publishes ``ActionExecuted`` for the audit trail, mirroring
        the normal execute path.
        """
        pending = self._pending_voice.pop(trace_id, None)
        if pending is None:
            return ToolResult(
                success=False,
                output=None,
                error="voice-confirm expired (no pending action for this turn)",
            )
        tool, args = pending
        ctx = ExecutionContext(
            trace_id=trace_id,
            user_utterance=user_utterance,
            config=config_snapshot or {},
            memory_read=memory_read,
            approved_by="user",
        )
        t_start = time.perf_counter()
        try:
            result = await tool.execute(args, ctx)
        except Exception as exc:  # noqa: BLE001
            duration_ms = int((time.perf_counter() - t_start) * 1000)
            await self._bus.publish(ActionExecuted(
                trace_id=trace_id,
                tool_name=tool.name,
                success=False,
                duration_ms=duration_ms,
                error=str(exc),
            ))
            return ToolResult(success=False, output=None, error=str(exc))
        self._record_result_origin(tool, trace_id, result, args)
        duration_ms = int((time.perf_counter() - t_start) * 1000)
        await self._bus.publish(ActionExecuted(
            trace_id=trace_id,
            tool_name=tool.name,
            success=result.success,
            duration_ms=duration_ms,
            error=result.error,
            output_preview=safe_preview(result.output),
        ))
        return result

    async def cancel_pending(self, trace_id: UUID, *, reason: str = "voice_vetoed") -> bool:
        """Drop the action stashed for ``trace_id`` ("nein"). Returns whether one
        existed. Publishes ``ActionDenied`` for the audit trail; ``reason`` says
        why, so a call that merely ended is not recorded as the user's veto."""
        pending = self._pending_voice.pop(trace_id, None)
        if pending is None:
            return False
        tool, _args = pending
        await self._bus.publish(ActionDenied(
            trace_id=trace_id,
            tool_name=tool.name,
            reason=reason,
        ))
        return True

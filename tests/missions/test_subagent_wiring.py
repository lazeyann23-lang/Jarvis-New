"""Run production mission fan-out with a deterministic worker provider."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiosqlite
import pytest

from jarvis.brain.tool_gateway import BrainSupervisorToolGateway
from jarvis.core import runtime_refs
from jarvis.core.bus import EventBus
from jarvis.core.config import JarvisConfig, SafetyConfig
from jarvis.missions import init as mission_init
from jarvis.missions.events import WorkerDraftReady, WorkerKilled
from jarvis.missions.kontrollierer.decomposer import Step
from jarvis.missions.manager import MissionManager
from jarvis.missions.state_machine import MissionState
from jarvis.missions.workers import broker_stdio
from jarvis.missions.workers.capabilities import RESTRICTED_WORKER_KNOWLEDGE_TOOLS
from jarvis.missions.workers.worker_tool_broker import _BROKER
from jarvis.safety import ApprovalWorkflow, RiskTierEvaluator, ToolExecutor
from jarvis.ui.web.server import WebServer
from tests.fakes.fake_mission_runtime import NoopJob, ResultEvent


async def test_existing_missions_and_nested_dispatches_keep_fanout_disabled(tmp_path: Path) -> None:
    db_path = tmp_path / "missions.db"
    manager = MissionManager(db_path)
    await manager.start()
    try:
        legacy_id = await manager.dispatch(prompt="A mission from an earlier version")
        await manager.transition_state(legacy_id, MissionState.RUNNING, reason="test")
    finally:
        await manager.stop()
    async with aiosqlite.connect(db_path) as connection:
        await connection.execute("ALTER TABLE missions DROP COLUMN subagents_enabled")
        await connection.commit()

    await manager.start()
    try:
        assert await manager.store.get_subagent_prompt(legacy_id) is None
        nested_id = await manager.dispatch(
            prompt="Nested mission",
            parent_mission_id=legacy_id,
            subagents_enabled=True,
        )
        await manager.transition_state(nested_id, MissionState.RUNNING, reason="test")
        assert await manager.store.get_subagent_prompt(nested_id) is None
    finally:
        await manager.stop()


@pytest.mark.parametrize("cancel_parent", [False, True])
async def test_opt_in_fanout_through_production_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_parent: bool
) -> None:
    child_started = asyncio.Event()
    child_stopped = asyncio.Event()
    recorded_results: list[dict[str, Any]] = []

    class Provider:
        cli = "claude"
        last_pid = 4242

        def __init__(self, inventory: Any) -> None:
            self.capability_inventory = inventory

        async def spawn(
            self, prompt: str, *, log_dir: Path, **kwargs: Any
        ) -> AsyncIterator[ResultEvent]:
            yield ResultEvent()
            binding = kwargs["_broker_binding"]
            if "::sub" in kwargs["worker_id"]:
                assert binding.tool_names == ()
                assert self.capability_inventory.native_tool_names == (
                    *RESTRICTED_WORKER_KNOWLEDGE_TOOLS, "search_web"
                )
                assert not self.capability_inventory.app_commands
                assert not self.capability_inventory.mcp_server_ids
                assert "Your sub-task: independently summarize the evidence" in prompt
                child_started.set()
                try:
                    if cancel_parent:
                        await asyncio.Event().wait()
                    (log_dir / "stream.jsonl").write_text(
                        json.dumps({"type": "result", "result": "Child delivered"}),
                        encoding="utf-8",
                    )
                finally:
                    child_stopped.set()
                return
            if "user enabled sub-agents" not in prompt:
                assert binding.tool_names == ()
                return
            assert binding.tool_names == ("spawn_subagents",)
            for key, value in binding.apply_environment({}).items():
                monkeypatch.setenv(key, value)
            listed = await broker_stdio.list_tools()
            assert [tool.name for tool in listed] == ["spawn_subagents"]
            invalid = await broker_stdio.call_tool("spawn_subagents", {"prompts": "invalid"})
            assert invalid.isError
            excess = await broker_stdio.call_tool("spawn_subagents", {"prompts": ["part"] * 11})
            assert "10 sub-agents in total" in excess.structuredContent["error"]
            result = await broker_stdio.call_tool(
                "spawn_subagents", {"prompts": ["independently summarize the evidence"]}
            )
            assert not result.isError
            recorded_results.append(result.structuredContent)

    monkeypatch.setattr(mission_init, "_default_job_factory", NoopJob)
    monkeypatch.setattr(mission_init, "_select_subagent_worker_kind", lambda *_: "api_agent")
    monkeypatch.setattr(
        mission_init,
        "_resolve_api_agent_worker",
        lambda _provider, _task, inventory, **_kwargs: Provider(inventory),
    )
    stack = await mission_init.bootstrap_missions(
        db_path=tmp_path / "missions.db",
        isolation_root=tmp_path / "outputs",
        repo_root=tmp_path,
        cleanup_startup_sweep=False,
    )
    manager = stack["manager"]
    supervisor = stack["kontrollierer"]
    bus = EventBus()
    for slot in (
        "_SUPERVISOR_TOOL_GATEWAY",
        "_MISSION_TOOL_AUTO_APPROVER",
        "_MISSION_TOOL_EXECUTION_CHECKER",
        "_MISSION_TOOL_EXECUTION_LEASE_CHECKER",
        "_MISSION_TOOL_EXECUTION_CLAIMER",
        "_MISSION_TOOL_EXECUTION_ACTIVATOR",
        "_MISSION_TOOL_EXECUTION_DISPATCH_FENCE",
        "_MISSION_TOOL_EXECUTION_FINISHER",
        "_WEB_APP",
    ):
        monkeypatch.setattr(runtime_refs, slot, [])
    web = WebServer(JarvisConfig(), bus=bus, defer_feature_routes=True)
    web.app.state.mission_manager = manager
    tool_executor = ToolExecutor(bus, RiskTierEvaluator(SafetyConfig()), ApprovalWorkflow(bus))
    gateway = BrainSupervisorToolGateway(SimpleNamespace(_tools={}, _tool_executor=tool_executor))
    runtime_refs.set_supervisor_tool_gateway(gateway)

    async def spawn(prompt: str, *, enabled: bool) -> tuple[str, asyncio.Task[Any]]:
        mission_id = await manager.dispatch(prompt=prompt, subagents_enabled=enabled)
        await manager.transition_state(mission_id, MissionState.RUNNING, reason="test")
        await manager.transition_state(mission_id, MissionState.CRITIQUING, reason="test")
        step = Step(slug="parent", prompt="Use 50 agents")
        path = tmp_path / mission_id
        path.mkdir()
        task = asyncio.create_task(supervisor._spawn_worker_collect(
            worker=supervisor._worker_factory(step),
            worker_prompt=step.prompt,
            worktree=path,
            mission_dir=path,
            log_dir=path,
            mission_id=mission_id,
            step=step,
            iteration=0,
            resume_session_id=None,
        ))
        return mission_id, task

    parent = None
    try:
        assert gateway.catalog() == ()
        plain_id, plain = await spawn("No delegation", enabled=False)
        await plain
        assert await manager.store.get_subagent_prompt(plain_id) is None
        original_prompt = "Please summarize the evidence"
        mission_id, parent = await spawn(original_prompt, enabled=True)
        assert await manager.store.get_subagent_prompt(mission_id) == original_prompt
        await asyncio.wait_for(child_started.wait(), 5)
        if cancel_parent:
            parent.cancel()
            with pytest.raises(asyncio.CancelledError):
                await parent
        else:
            await asyncio.wait_for(parent, 5)
            assert "Child delivered" in recorded_results[0]["output"]["combined"]
        await asyncio.wait_for(child_stopped.wait(), 5)
        events = await manager.store.events_for_mission(mission_id)
        child_terminal = [
            event.payload for event in events
            if "::sub" in (event.worker_id or "")
            and isinstance(event.payload, (WorkerDraftReady, WorkerKilled))
        ]
        assert len(child_terminal) == 1
        assert isinstance(child_terminal[0], WorkerKilled if cancel_parent else WorkerDraftReady)
    finally:
        if parent is not None and not parent.done():
            parent.cancel()
            with suppress(asyncio.CancelledError):
                await parent
        _BROKER.reset_for_tests()
        await mission_init.shutdown_missions(stack)

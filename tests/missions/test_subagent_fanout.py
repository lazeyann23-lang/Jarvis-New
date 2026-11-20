"""Fan-out preserves isolation and the worker lifecycle across parallel batches."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.missions import subagent_fanout
from jarvis.missions.events import WorkerDraftReady, WorkerKilled, WorkerSpawned
from jarvis.missions.ids import uuid7_str
from tests.fakes.fake_mission_runtime import NoopJob, ResultEvent


class FakeFanout:
    """Worker factory and event sink with a deterministic concurrency barrier."""

    cli = "claude"
    last_pid = 4242

    def __init__(self, *, blocked: bool = False, capacity: int = 1) -> None:
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()
        self.capacity = capacity
        self.ready = asyncio.Event()
        self.active = 0
        self.peak = 0
        self.events = []

    async def spawn(self, prompt: str, *, worktree: Path, env: dict[str, str], log_dir: Path, **_):
        assert env["isolated_workspace"] == str(worktree)
        self.active += 1
        self.peak = max(self.peak, self.active)
        if self.active == self.capacity:
            self.ready.set()
        try:
            yield ResultEvent()
            await self.release.wait()
            task = prompt.rsplit("Your sub-task: ", 1)[1].splitlines()[0]
            if task == "crash":
                raise RuntimeError("worker stopped after its first event")
            (log_dir / "stream.jsonl").write_text(
                json.dumps({"type": "result", "result": task}) + "\n",
                encoding="utf-8",
            )
        finally:
            self.active -= 1

    async def append_and_publish(self, envelope) -> None:
        assert envelope.worker_id == envelope.payload.worker_id
        self.events.append(envelope.payload)

    def runner(self, root: Path) -> subagent_fanout.SubagentFanoutRunner:
        return subagent_fanout.SubagentFanoutRunner(
            worker_factory=lambda _step, _inventory: self,
            env_builder=lambda workspace: {"isolated_workspace": str(workspace)},
            job_factory=NoopJob,
            isolation_root=root,
            manager=SimpleNamespace(store=self),
            child_inventory_builder=lambda _prompt: None,
        )


async def test_parallel_batches_and_new_runner_reserve_distinct_workspaces(tmp_path):
    mission_id = uuid7_str()
    fake = FakeFanout()
    runner = fake.runner(tmp_path)

    first, second = await asyncio.gather(
        runner.run(mission_id=mission_id, prompts=["alpha", "beta"]),
        runner.run(mission_id=mission_id, prompts=["gamma", "delta"]),
    )
    next_round = await fake.runner(tmp_path).run(mission_id=mission_id, prompts=["epsilon"])
    results = first + second + next_round

    assert [result.answer for result in results] == ["alpha", "beta", "gamma", "delta", "epsilon"]
    assert all(result.ok for result in results)
    assert {result.index for result in results} == set(range(5))
    assert next_round[0].index == 4
    assert len({result.workspace for result in results}) == 5
    spawns = [event for event in fake.events if isinstance(event, WorkerSpawned)]
    assert len({event.worker_id for event in spawns}) == 5


async def test_child_slots_are_shared_by_concurrent_missions(monkeypatch, tmp_path):
    monkeypatch.setattr(subagent_fanout, "_MAX_CONCURRENT_CHILDREN", 2)
    fake = FakeFanout(blocked=True, capacity=2)
    runner = fake.runner(tmp_path)
    first = asyncio.create_task(runner.run(mission_id=uuid7_str(), prompts=["alpha", "beta"]))
    second = asyncio.create_task(runner.run(mission_id=uuid7_str(), prompts=["gamma", "delta"]))

    try:
        await asyncio.wait_for(fake.ready.wait(), 2)
        assert fake.active == 2
    finally:
        fake.release.set()
        await asyncio.gather(first, second)

    assert fake.peak == 2
    assert fake.active == 0


async def test_same_millisecond_missions_use_separate_child_state(tmp_path):
    fake = FakeFanout()
    runner = fake.runner(tmp_path)
    left, right = await asyncio.gather(
        runner.run(mission_id="018f0000-0000-7000-8000-000000000001", prompts=["alpha"]),
        runner.run(mission_id="018f0000-0000-7000-8000-000000000002", prompts=["beta"]),
    )
    assert left[0].index == right[0].index == 0
    assert left[0].workspace != right[0].workspace
    spawns = [event for event in fake.events if isinstance(event, WorkerSpawned)]
    assert len({event.worker_id for event in spawns}) == 2


async def test_worker_crash_records_terminal_event_and_sibling_completes(tmp_path):
    fake = FakeFanout()
    failed, healthy = await fake.runner(tmp_path).run(
        mission_id=uuid7_str(), prompts=["crash", "healthy"],
    )

    assert failed.ok is False
    assert "after its first event" in failed.error
    assert healthy.ok and healthy.answer == "healthy"
    spawn, = (
        event for event in fake.events
        if isinstance(event, WorkerSpawned) and event.worktree == failed.workspace
    )
    lifecycle = [event for event in fake.events if event.worker_id == spawn.worker_id]
    assert [type(event) for event in lifecycle] == [WorkerSpawned, WorkerKilled]
    assert lifecycle[-1].reason == "worker_error"


async def test_parent_cancel_marks_active_child_and_leaves_queued_child_unstarted(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(subagent_fanout, "_MAX_CONCURRENT_CHILDREN", 1)
    fake = FakeFanout(blocked=True)
    runner = fake.runner(tmp_path)
    mission_id = uuid7_str()
    batch = asyncio.create_task(
        runner.run(mission_id=mission_id, prompts=["active", "queued"]),
    )

    try:
        await asyncio.wait_for(fake.ready.wait(), 2)
    finally:
        batch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await batch

    assert fake.active == 0
    assert [type(event) for event in fake.events] == [WorkerSpawned, WorkerKilled]
    assert fake.events[-1].reason == "parent_cancelled"
    assert fake.events[0].worker_id == fake.events[-1].worker_id

    fake.release.set()
    next_round = await asyncio.wait_for(
        runner.run(mission_id=mission_id, prompts=["next"]), 2,
    )
    assert next_round[0].ok and next_round[0].index == 1
    assert isinstance(fake.events[-1], WorkerDraftReady)

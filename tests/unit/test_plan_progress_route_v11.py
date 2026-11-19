"""HTTP contract test; requires the normal runtime database dependency."""
from __future__ import annotations

import importlib.util

import httpx
import pytest
from fastapi import FastAPI

from jarvis.missions.events import EventEnvelope, MissionPlanReady


@pytest.mark.asyncio
async def test_progress_route_reads_store_without_exposing_prompts():
    if importlib.util.find_spec("aiosqlite") is None:
        pytest.skip("aiosqlite not installed in this test environment")
    from jarvis.ui.web.missions_routes import router

    class Store:
        async def get_mission_view(self, mission_id):
            if mission_id != "m1":
                return None
            return ("private prompt", "RUNNING", "de", 1, 0.0)

        async def events_for_mission(self, mission_id):
            return [EventEnvelope(mission_id="m1", source_actor="system", ts_ms=1,
                                  payload=MissionPlanReady(plan=[
                                      {"task_id": "task-1", "slug": "analyse", "prompt": "PRIVATE_TOKEN"}
                                  ], n_workers=1))]

    class Manager:
        store = Store()

    app = FastAPI()
    app.state.mission_manager = Manager()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/missions/m1/progress")
        missing = await client.get("/api/missions/missing/progress")
    assert response.status_code == 200
    assert response.json()["steps"][0]["status"] == "queued"
    assert "PRIVATE_TOKEN" not in response.text
    assert missing.status_code == 404

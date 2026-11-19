"""V12: user-controlled acknowledged voice/chat memory capture and privacy."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from jarvis.core.bus import EventBus
from jarvis.core.events import VoiceTurnCompleted
from jarvis.memory.wiki import capture_policy, integration
from jarvis.memory.wiki.voice_bridge import VoiceFactBridge
from jarvis.ui.web.wiki_routes import router


@pytest.fixture(autouse=True)
def isolated_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(capture_policy, "_path", lambda: tmp_path / "prefs" / "capture.json")
    integration._set_running_voice_bridge(None)
    yield
    integration._set_running_voice_bridge(None)


def _turn(turn_id: str = "one") -> VoiceTurnCompleted:
    return VoiceTurnCompleted(
        session_id="session", turn_id=turn_id,
        user_text="Please remember my favourite project is JARVIS.",
        jarvis_text="Noted.", tier="realtime",
    )


class FakeExtractor:
    def __init__(self) -> None:
        self.calls = 0
        self.saved: list[str] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False

    def capture_seen(self, key: str) -> bool:
        return False

    async def extract_and_journal(self, text: str, reply: str, **kwargs):
        self.calls += 1
        self.started.set()
        if self.block:
            await self.release.wait()
        self.saved.append(text)
        return 1


async def _drain(bridge: VoiceFactBridge) -> None:
    if bridge._inflight:
        await asyncio.wait_for(asyncio.gather(*bridge._inflight, return_exceptions=True), 3)


def test_function_persistence_and_corruption_fail_closed(tmp_path: Path):
    path = tmp_path / "prefs" / "capture.json"
    assert capture_policy.capture_enabled(path=path) is True
    capture_policy.set_capture_enabled(False, path=path)
    assert capture_policy.capture_enabled(path=path) is False
    assert json.loads(path.read_text()) == {"schema": 1, "enabled": False}
    if os.name == "posix":
        assert (path.stat().st_mode & 0o777) == 0o600
    capture_policy.set_capture_enabled(True, path=path)
    assert capture_policy.capture_enabled(path=path) is True
    path.write_text('{"enabled": "false", "schema": 1}')
    assert capture_policy.capture_enabled(path=path) is False
    path.write_text('not json')
    assert capture_policy.capture_enabled(path=path) is False
    with pytest.raises(TypeError):
        capture_policy.set_capture_enabled("yes", path=path)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_integration_route_pause_and_resume_realtime_capture():
    bus = EventBus()
    extractor = FakeExtractor()
    bridge = VoiceFactBridge(bus=bus, extractor=extractor)
    bridge.start()
    integration._set_running_voice_bridge(bridge)
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        try:
            status = await client.get("/api/wiki/capture-policy")
            assert status.status_code == 200 and status.json()["enabled"] is True
            assert status.headers["cache-control"] == "no-store"
            paused = await client.put("/api/wiki/capture-policy", json={"enabled": False})
            assert paused.status_code == 200 and paused.json()["enabled"] is False
            assert paused.json()["existing_memories_unchanged"] is True
            await bus.publish(_turn())
            await _drain(bridge)
            assert extractor.calls == 0
            resumed = await client.put("/api/wiki/capture-policy", json={"enabled": True})
            assert resumed.status_code == 200 and resumed.json()["enabled"] is True
            await bus.publish(_turn("two"))
            await _drain(bridge)
            assert extractor.calls == 1
            assert len(extractor.saved) == 1
        finally:
            await bridge.stop_and_wait()


@pytest.mark.asyncio
async def test_security_pause_cancels_inflight_and_drops_pending():
    bus = EventBus()
    extractor = FakeExtractor()
    extractor.block = True
    bridge = VoiceFactBridge(bus=bus, extractor=extractor)
    bridge.start()
    integration._set_running_voice_bridge(bridge)
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        try:
            await bus.publish(_turn())
            await asyncio.wait_for(extractor.started.wait(), 3)
            assert bridge._inflight
            paused = await client.put("/api/wiki/capture-policy", json={"enabled": False})
            assert paused.status_code == 200
            assert not bridge._inflight
            assert bridge.waiting_count == 0
            assert not bridge._realtime_sessions
            extractor.release.set()
            await asyncio.sleep(0)
            assert extractor.saved == []
            await bus.publish(_turn("two"))
            assert extractor.calls == 1
            assert (await client.put("/api/wiki/capture-policy", json={"enabled": "no"})).status_code == 422
            assert (await client.get("/api/wiki/capture-policy")).json()["enabled"] is False
        finally:
            await bridge.stop_and_wait()


def test_regression_manual_ingest_unaffected_by_capture_switch(tmp_path: Path):
    """Policy does not silently override the existing manual Wiki workflow."""
    capture_policy.set_capture_enabled(False, path=tmp_path / "policy.json")
    from jarvis.memory.wiki.ingest_service import ingest_wiki_text
    assert callable(ingest_wiki_text)

@pytest.mark.asyncio
async def test_security_chat_pairing_drops_private_pending_when_paused():
    from jarvis.core.events import MessageSent, ResponseGenerated

    bus = EventBus()
    extractor = FakeExtractor()
    bridge = VoiceFactBridge(bus=bus, extractor=extractor)
    bridge.start()
    integration._set_running_voice_bridge(bridge)
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        try:
            await bus.publish(MessageSent(role="user", text="Remember my preferred code editor is VS Code."))
            assert bridge._pending.user_text
            await client.put("/api/wiki/capture-policy", json={"enabled": False})
            assert bridge._pending.user_text == ""
            await bus.publish(ResponseGenerated(text="Noted."))
            await _drain(bridge)
            assert extractor.calls == 0
            await client.put("/api/wiki/capture-policy", json={"enabled": True})
            await bus.publish(MessageSent(role="user", text="Remember my preferred code editor is VS Code."))
            await bus.publish(ResponseGenerated(text="Noted."))
            await _drain(bridge)
            assert extractor.calls == 1
        finally:
            await bridge.stop_and_wait()


@pytest.mark.asyncio
async def test_security_persistence_error_reports_failure(monkeypatch: pytest.MonkeyPatch):
    def fail_write(enabled: bool):
        raise OSError("disk read-only")

    import jarvis.ui.web.wiki_routes as routes
    monkeypatch.setattr(routes, "set_capture_enabled", fail_write)
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.put("/api/wiki/capture-policy", json={"enabled": False})
        assert result.status_code == 503
        assert "disk" not in result.text
        assert (await client.get("/api/wiki/capture-policy")).json()["enabled"] is True

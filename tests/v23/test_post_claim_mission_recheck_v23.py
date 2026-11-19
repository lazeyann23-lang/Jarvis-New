"""V23: post-claim lifecycle revalidation before positive mission approvals.

SQLite integration uses production SQL with a stdlib async adapter, not the
unavailable production aiosqlite driver. No external tools are executed.
"""
from __future__ import annotations

import sqlite3
import sys
import time
import types
from pathlib import Path
from uuid import uuid4

import pytest

try:
    import aiosqlite  # noqa: F401
except ImportError:
    shim = types.ModuleType("aiosqlite")
    for name in ("Connection", "Cursor", "Row"):
        setattr(shim, name, type(name, (), {}))
    sys.modules["aiosqlite"] = shim

from jarvis.core.bus import EventBus
from jarvis.core.events import ActionApprovalRequired, ActionApproved, ActionDenied
from jarvis.missions.event_store import MissionEventStore, _SCHEMA_PATH
from jarvis.missions.tool_approvals import MissionToolApprovalCoordinator, MissionToolAutoApprover


class AsyncCursor:
    def __init__(self, cursor):
        self.cursor = cursor

    async def fetchone(self):
        return self.cursor.fetchone()

    async def fetchall(self):
        return self.cursor.fetchall()

    async def close(self):
        self.cursor.close()


class AsyncSQLite:
    def __init__(self, connection):
        self.connection = connection

    async def execute(self, query, params=()):
        return AsyncCursor(self.connection.execute(query, params))


def database(path: Path):
    conn = sqlite3.connect(path, isolation_level=None, timeout=5)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def add_event(conn, state: str, mission: str = "m1"):
    conn.execute(
        "INSERT INTO mission_events "
        "(event_id,mission_id,event_type,source_actor,ts_ms,payload_json) "
        "VALUES (?,?, 'MissionStateChanged','ui',1,?)",
        (str(uuid4()), mission, '{"to_state":"' + state + '"}'),
    )


def add_mission(conn):
    conn.execute(
        "INSERT INTO missions (id,prompt,state,language,created_ms,updated_ms) "
        "VALUES ('m1','task','RUNNING','de',1,1)"
    )
    add_event(conn, "RUNNING")


def store(conn):
    s = MissionEventStore(Path("/unused"), bus=None)
    s._conn = AsyncSQLite(conn)
    return s


def request():
    return ActionApprovalRequired(
        trace_id=uuid4(), mission_id="m1", worker_id="w1",
        tool_name="external-action", risk_tier="ask", reason="risk_tier",
        args_preview="<redacted>",
        expires_at_ns=time.time_ns() + 30_000_000_000,
    )


def capture(bus):
    approvals, denials = [], []

    async def approved(event):
        approvals.append(event)

    async def denied(event):
        denials.append(event)

    bus.subscribe(ActionApproved, approved)
    bus.subscribe(ActionDenied, denied)
    return approvals, denials


def coordinator(bus, s, *, on_claim=None):
    async def durable(trace, mission, tool, decision):
        result = await s.try_claim_tool_decision(
            trace_id=str(trace), mission_id=mission,
            tool_name=tool, decision=decision,
        )
        if on_claim is not None:
            await on_claim()
        return result

    return MissionToolApprovalCoordinator(
        bus, mission_is_active=s.is_active_for_tool_approval,
        durable_claim=durable,
    )


# FUNCTION TESTS
@pytest.mark.asyncio
async def test_function_manual_approval_of_active_mission_still_works(tmp_path):
    conn = database(tmp_path / "mission.db")
    try:
        add_mission(conn)
        bus = EventBus()
        approvals, denials = capture(bus)
        coord = coordinator(bus, store(conn))
        event = request()
        await bus.publish(event)
        assert await coord.approve("m1", event.trace_id, approved_by="human") is not None
        assert len(approvals) == 1 and not denials
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_function_auto_approval_of_active_mission_still_works(tmp_path):
    conn = database(tmp_path / "mission.db")
    try:
        add_mission(conn)
        bus = EventBus()
        approvals, denials = capture(bus)
        coord = coordinator(bus, store(conn))
        auto = MissionToolAutoApprover(bus, coordinator=coord)
        auto.arm("m1", "grant", frozenset({"external-action"}))
        await bus.publish(request())
        assert len(approvals) == 1 and not denials
    finally:
        conn.close()


# INTEGRATION TESTS: a second connection commits cancellation after claim.
@pytest.mark.asyncio
async def test_integration_manual_claim_then_remote_cancel_before_publish(tmp_path):
    path = tmp_path / "mission.db"
    first, second = database(path), database(path)
    try:
        add_mission(first)

        async def cancel_after_claim():
            add_event(second, "CANCELLED")

        bus = EventBus()
        approvals, denials = capture(bus)
        coord = coordinator(bus, store(first), on_claim=cancel_after_claim)
        event = request()
        await bus.publish(event)
        assert await coord.approve("m1", event.trace_id, approved_by="human") is None
        assert not approvals
        assert len(denials) == 1
        assert denials[0].reason == "mission_inactive_after_claim"
        assert first.execute("SELECT decision FROM mission_tool_approval_decisions").fetchone() == ("approved",)
    finally:
        second.close()
        first.close()


@pytest.mark.asyncio
async def test_integration_auto_claim_then_remote_cancel_before_publish(tmp_path):
    path = tmp_path / "mission.db"
    first, second = database(path), database(path)
    try:
        add_mission(first)

        async def cancel_after_claim():
            add_event(second, "CANCELLED")

        bus = EventBus()
        approvals, denials = capture(bus)
        coord = coordinator(bus, store(first), on_claim=cancel_after_claim)
        auto = MissionToolAutoApprover(bus, coordinator=coord)
        auto.arm("m1", "grant", frozenset({"external-action"}))
        await bus.publish(request())
        assert not approvals
        assert len(denials) == 1
        assert denials[0].reason == "mission_inactive_after_claim"
        assert first.execute("SELECT decision FROM mission_tool_approval_decisions").fetchone() == ("auto_approved",)
    finally:
        second.close()
        first.close()


# SECURITY/REGRESSION TESTS
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["manual", "auto"])
async def test_security_second_status_check_failure_is_fail_closed(mode):
    bus = EventBus()
    approvals, denials = capture(bus)
    checks = 0

    async def flaky_state(_mission):
        nonlocal checks
        checks += 1
        if checks >= (3 if mode == "auto" else 2):
            raise OSError("DB unavailable after claim")
        return True

    async def durable(*_args):
        return True

    coord = MissionToolApprovalCoordinator(
        bus, mission_is_active=flaky_state, durable_claim=durable,
    )
    event = request()
    if mode == "manual":
        await bus.publish(event)
        assert await coord.approve("m1", event.trace_id, approved_by="human") is None
    else:
        auto = MissionToolAutoApprover(bus, coordinator=coord)
        auto.arm("m1", "grant", frozenset({"external-action"}))
        await bus.publish(event)
    assert not approvals
    assert len(denials) == 1
    assert denials[0].reason == "mission_inactive_after_claim"


@pytest.mark.asyncio
async def test_regression_legacy_coordinator_without_durable_reader():
    bus = EventBus()
    approvals, denials = capture(bus)
    coord = MissionToolApprovalCoordinator(bus)
    event = request()
    await bus.publish(event)
    assert await coord.approve("m1", event.trace_id, approved_by="human") is not None
    assert len(approvals) == 1 and not denials

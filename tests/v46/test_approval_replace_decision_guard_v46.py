"""V46: existing approval decisions resist SQLite INSERT OR REPLACE.

Production schema and MissionEventStore SQL are exercised with real sqlite3;
only the async driver is replaced by the existing test-only stdlib adapter.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest

from tests.v22.test_durable_tool_decisions_v22 import (
    add_mission, claim, database, store,
)
from jarvis.missions.event_store import _SCHEMA_PATH

pytestmark = pytest.mark.asyncio


def decision_row(conn, trace):
    return conn.execute(
        "SELECT trace_id, mission_id, tool_name, decision, claimed_ms "
        "FROM mission_tool_approval_decisions WHERE trace_id=?", (trace,)
    ).fetchone()


def replace(conn, trace, *, decision="approved", mission="m1", tool="changed-tool"):
    conn.execute(
        "INSERT OR REPLACE INTO mission_tool_approval_decisions "
        "(trace_id, mission_id, tool_name, decision, claimed_ms) "
        "VALUES (?, ?, ?, ?, 999)", (trace, mission, tool, decision)
    )


async def reserve(conn, *, decision="denied"):
    trace = str(uuid4())
    assert await claim(store(conn), trace, decision=decision)
    return trace


# FUNCTION (3)
async def test_function_replace_cannot_turn_denial_into_approval(tmp_path):
    c = database(tmp_path / "db")
    try:
        add_mission(c)
        trace = await reserve(c)
        before = decision_row(c, trace)
        replace(c, trace, decision="approved")
        assert decision_row(c, trace) == before
    finally:
        c.close()


async def test_function_replace_cannot_change_existing_approval_or_tool(tmp_path):
    c = database(tmp_path / "db")
    try:
        add_mission(c)
        trace = await reserve(c, decision="auto_approved")
        before = decision_row(c, trace)
        replace(c, trace, decision="denied", mission="other-mission")
        assert decision_row(c, trace) == before
    finally:
        c.close()


async def test_function_normal_claims_still_first_writer_wins(tmp_path):
    c = database(tmp_path / "db")
    try:
        add_mission(c)
        trace = await reserve(c)
        assert not await claim(store(c), trace, decision="approved")
        assert await claim(store(c), str(uuid4()), decision="approved")
        assert c.execute("SELECT count(*) FROM mission_tool_approval_decisions").fetchone()[0] == 2
    finally:
        c.close()


# INTEGRATION (3)
async def test_integration_independent_sqlite_connections_preserve_denial(tmp_path):
    path = tmp_path / "db"
    a, b = database(path), database(path)
    try:
        add_mission(a)
        trace = await reserve(a)
        before = decision_row(a, trace)
        replace(b, trace)
        assert decision_row(a, trace) == decision_row(b, trace) == before
    finally:
        a.close(); b.close()


async def test_integration_separate_os_process_cannot_replace_denial(tmp_path):
    path = tmp_path / "db"
    c = database(path)
    try:
        add_mission(c)
        trace = await reserve(c)
        before = decision_row(c, trace)
        code = (
            "import sqlite3,sys; c=sqlite3.connect(sys.argv[1],isolation_level=None); "
            "c.execute('INSERT OR REPLACE INTO mission_tool_approval_decisions '"
            "'(trace_id,mission_id,tool_name,decision,claimed_ms) '"
            "'VALUES (?,\"m1\",\"bad\",\"approved\",999)',(sys.argv[2],)); c.close()"
        )
        p = subprocess.run([sys.executable, "-c", code, str(path), trace],
                           capture_output=True, text=True, timeout=8)
        assert p.returncode == 0, p.stderr
        assert decision_row(c, trace) == before
    finally:
        c.close()


async def test_integration_replace_in_transaction_and_rollback(tmp_path):
    path = tmp_path / "db"
    a, b = database(path), database(path)
    try:
        add_mission(a)
        trace = await reserve(a)
        before = decision_row(a, trace)
        fresh = str(uuid4())
        b.execute("BEGIN IMMEDIATE")
        replace(b, trace)
        replace(b, fresh)
        assert decision_row(b, trace) == before
        assert decision_row(a, trace) == before
        assert decision_row(b, fresh) is not None
        b.rollback()
        assert decision_row(a, trace) == before
        assert decision_row(a, fresh) is None
    finally:
        if b.in_transaction: b.rollback()
        a.close(); b.close()


# SECURITY / REGRESSION (4)
async def test_security_replace_does_not_create_tombstone_or_replay_window(tmp_path):
    c = database(tmp_path / "db")
    try:
        add_mission(c)
        trace = await reserve(c)
        before = decision_row(c, trace)
        replace(c, trace)
        assert decision_row(c, trace) == before
        assert c.execute("SELECT count(*) FROM mission_tool_approval_tombstones "
                         "WHERE trace_id=?", (trace,)).fetchone()[0] == 0
        assert not await claim(store(c), trace, decision="approved")
    finally:
        c.close()


async def test_security_multirow_replace_ignores_duplicate_but_adds_new(tmp_path):
    c = database(tmp_path / "db")
    try:
        add_mission(c)
        trace = await reserve(c)
        before = decision_row(c, trace)
        fresh = str(uuid4())
        c.execute(
            "INSERT OR REPLACE INTO mission_tool_approval_decisions "
            "(trace_id,mission_id,tool_name,decision,claimed_ms) "
            "VALUES (?,'m1','bad','approved',999), (?,'m1','new','denied',10)",
            (trace, fresh),
        )
        assert decision_row(c, trace) == before
        assert decision_row(c, fresh)[2:] == ("new", "denied", 10)
    finally:
        c.close()


async def test_security_schema_reload_installs_guard_on_existing_db(tmp_path):
    c = database(tmp_path / "db")
    try:
        add_mission(c)
        trace = await reserve(c)
        before = decision_row(c, trace)
        c.execute("DROP TRIGGER IF EXISTS trg_mission_approval_no_duplicate_replace")
        c.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        replace(c, trace)
        assert decision_row(c, trace) == before
    finally:
        c.close()


async def test_security_replace_into_syntax_cannot_override_denial(tmp_path):
    c = database(tmp_path / "db")
    try:
        add_mission(c)
        trace = await reserve(c)
        before = decision_row(c, trace)
        c.execute(
            "REPLACE INTO mission_tool_approval_decisions "
            "(trace_id,mission_id,tool_name,decision,claimed_ms) "
            "VALUES (?,'m1','bad','approved',999)", (trace,)
        )
        assert decision_row(c, trace) == before
    finally:
        c.close()

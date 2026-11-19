"""V36 terminal-only approval retention; real SQLite SQL, test-only async shim."""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest

from tests.v22.test_durable_tool_decisions_v22 import (
    add_event, add_mission, claim, database, store,
)

pytestmark = pytest.mark.asyncio


def rows(conn):
    return {r[0] for r in conn.execute('SELECT trace_id FROM mission_tool_approval_decisions')}


def tombs(conn):
    return {r[0] for r in conn.execute('SELECT trace_id FROM mission_tool_approval_tombstones')}


def terminal(conn, mission='m1', state='CANCELLED', event=True):
    if event:
        add_event(conn, mission, state)
    conn.execute('UPDATE missions SET state=? WHERE id=?', (state, mission))


async def reserve(conn, decision='auto_approved'):
    trace = str(uuid4())
    assert await claim(store(conn), trace, decision=decision)
    return trace


# FUNCTION (3)
async def test_function_terminal_only_and_cutoff(tmp_path):
    c = database(tmp_path/'db')
    try:
        add_mission(c)
        old, recent = await reserve(c), await reserve(c, 'denied')
        c.execute('UPDATE mission_tool_approval_decisions SET claimed_ms=10 WHERE trace_id=?', (old,))
        c.execute('UPDATE mission_tool_approval_decisions SET claimed_ms=1000 WHERE trace_id=?', (recent,))
        assert await store(c).compact_terminal_tool_decisions(before_ms=100) == 0
        terminal(c)
        assert await store(c).compact_terminal_tool_decisions(before_ms=100) == 1
        assert tombs(c) == {old} and rows(c) == {recent}
    finally: c.close()


async def test_function_bounded_batches_and_no_reclaim(tmp_path):
    c = database(tmp_path/'db')
    try:
        add_mission(c)
        traces = {await reserve(c) for _ in range(5)}
        terminal(c)
        s = store(c)
        assert [await s.compact_terminal_tool_decisions(before_ms=10**15, limit=2)
                for _ in range(4)] == [2, 2, 1, 0]
        assert tombs(c) == traces
        # Even if the mission is subsequently (incorrectly) resumed, no replay.
        c.execute("UPDATE missions SET state='RUNNING' WHERE id='m1'")
        add_event(c, 'm1', 'RUNNING')
        for trace in traces:
            assert not await claim(s, trace)
    finally: c.close()


async def test_function_requires_both_terminal_header_and_event(tmp_path):
    c = database(tmp_path/'db')
    try:
        add_mission(c)
        trace = await reserve(c)
        terminal(c, event=False)
        assert await store(c).compact_terminal_tool_decisions(before_ms=10**15) == 0
        add_event(c, 'm1', 'CANCELLED')
        assert await store(c).compact_terminal_tool_decisions(before_ms=10**15) == 1
        assert tombs(c) == {trace}
    finally: c.close()


# INTEGRATION (3)
async def test_integration_two_connections_cannot_reclaim(tmp_path):
    path = tmp_path/'db'; a, b = database(path), database(path)
    try:
        add_mission(a)
        trace = await reserve(a)
        terminal(a)
        assert await store(b).compact_terminal_tool_decisions(before_ms=10**15) == 1
        assert tombs(a) == {trace}
        a.execute("UPDATE missions SET state='RUNNING' WHERE id='m1'")
        add_event(a, 'm1', 'RUNNING')
        assert not await claim(store(a), trace)
    finally: a.close(); b.close()


async def test_integration_rollback_keeps_original_decision(tmp_path):
    path = tmp_path/'db'; a, b = database(path), database(path)
    try:
        add_mission(a); trace = await reserve(a); terminal(a)
        b.execute('BEGIN IMMEDIATE')
        assert await store(b).compact_terminal_tool_decisions(before_ms=10**15) == 1
        assert tombs(b) == {trace} and tombs(a) == set()
        b.execute('ROLLBACK')
        assert rows(a) == {trace} and tombs(a) == set()
        assert await store(a).compact_terminal_tool_decisions(before_ms=10**15) == 1
    finally: a.close(); b.close()


async def test_integration_independent_process_cannot_reclaim(tmp_path):
    path = tmp_path/'db'; c = database(path)
    try:
        add_mission(c); trace = await reserve(c); terminal(c)
        assert await store(c).compact_terminal_tool_decisions(before_ms=10**15) == 1
        code = ("import sqlite3,sys; c=sqlite3.connect(sys.argv[1],isolation_level=None); "
                "c.execute(\"INSERT INTO mission_tool_approval_decisions "
                "(trace_id,mission_id,tool_name,decision,claimed_ms) "
                "VALUES (?, 'm1', 'external-action', 'approved', 1)\",(sys.argv[2],)); "
                "print(c.execute('SELECT count(*) FROM mission_tool_approval_decisions "
                "WHERE trace_id=?',(sys.argv[2],)).fetchone()[0]); c.close()")
        p = subprocess.run([sys.executable,'-c',code,str(path),trace],
                           capture_output=True,text=True,check=True,timeout=8)
        assert p.stdout.strip() == '0'
    finally: c.close()


# SECURITY / REGRESSION (4)
async def test_security_invalid_parameters_fail_closed(tmp_path):
    c = database(tmp_path/'db')
    try:
        add_mission(c); trace = await reserve(c); terminal(c)
        for kwargs in ({'before_ms':-1}, {'before_ms':True}, {'before_ms':1.5},
                       {'before_ms':10**15,'limit':0},
                       {'before_ms':10**15,'limit':1001},
                       {'before_ms':10**15,'limit':True}):
            with pytest.raises(ValueError):
                await store(c).compact_terminal_tool_decisions(**kwargs)
        assert rows(c) == {trace} and tombs(c) == set()
    finally: c.close()


async def test_security_immutable_trace_and_tombstone(tmp_path):
    c = database(tmp_path/'db')
    try:
        add_mission(c); trace = await reserve(c)
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            c.execute('UPDATE mission_tool_approval_decisions SET trace_id=? WHERE trace_id=?',
                      (str(uuid4()), trace))
        terminal(c)
        assert await store(c).compact_terminal_tool_decisions(before_ms=10**15) == 1
        with pytest.raises(sqlite3.IntegrityError, match='tombstones'):
            c.execute('DELETE FROM mission_tool_approval_tombstones WHERE trace_id=?', (trace,))
        with pytest.raises(sqlite3.IntegrityError, match='tombstones'):
            c.execute('UPDATE mission_tool_approval_tombstones SET trace_id=? WHERE trace_id=?',
                      (str(uuid4()), trace))
        assert tombs(c) == {trace}
    finally: c.close()


async def test_security_no_compaction_with_missing_or_corrupt_lifecycle(tmp_path):
    c = database(tmp_path/'db')
    try:
        add_mission(c); trace = await reserve(c)
        c.execute("UPDATE missions SET state='CANCELLED' WHERE id='m1'")
        add_event(c, 'm1', 'RUNNING', payload='{invalid')
        assert await store(c).compact_terminal_tool_decisions(before_ms=10**15) == 0
        assert rows(c) == {trace}
        # Even an older terminal event is insufficient when the latest event
        # contradicts it (e.g. after a partial/stale header projection).
        add_event(c, 'm1', 'CANCELLED')
        add_event(c, 'm1', 'RUNNING')
        assert await store(c).compact_terminal_tool_decisions(before_ms=10**15) == 0
        # The missing header also must not make a row eligible.
        c.execute("DELETE FROM missions WHERE id='m1'")
        assert await store(c).compact_terminal_tool_decisions(before_ms=10**15) == 0
    finally: c.close()


async def test_security_manual_denial_stays_denied_after_retention(tmp_path):
    c = database(tmp_path/'db')
    try:
        add_mission(c); trace = await reserve(c, 'denied'); terminal(c)
        assert await store(c).compact_terminal_tool_decisions(before_ms=10**15) == 1
        c.execute("UPDATE missions SET state='RUNNING' WHERE id='m1'")
        add_event(c, 'm1', 'RUNNING')
        assert not await claim(store(c), trace, decision='approved')
        assert rows(c) == set() and tombs(c) == {trace}
    finally: c.close()

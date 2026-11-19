"""V44: a committed per-trace cancellation latch cannot be cleared/replaced.

Uses real SQLite (two connections and an independent OS process). The
async adapter is test-only; no external tool side effects are executed.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest

from tests.v25.test_mission_execution_leases_v25 import (
    add_event, add_mission, database, store,
)
from jarvis.missions.event_store import _SCHEMA_PATH

pytestmark = pytest.mark.asyncio


async def reserve(s, mission='m1', tool='external-action'):
    trace = str(uuid4())
    assert await s.try_start_tool_execution(
        trace_id=trace, mission_id=mission, tool_name=tool)
    assert await s.try_activate_tool_execution(
        trace_id=trace, mission_id=mission, tool_name=tool)
    return trace


def latch(c, trace):
    return c.execute(
        'SELECT cancel_requested_ms FROM mission_tool_execution_leases '
        'WHERE trace_id=?', (trace,)
    ).fetchone()[0]


def pause(c):
    c.execute("UPDATE missions SET state='WAITING_CAPACITY' WHERE id='m1'")


def resume(c):
    c.execute("UPDATE missions SET state='RUNNING' WHERE id='m1'")


# FUNCTION (3)
async def test_function_committed_latch_cannot_be_cleared(tmp_path):
    c = database(tmp_path/'m.db')
    try:
        add_mission(c); s = store(c); trace = await reserve(s)
        pause(c)
        original = latch(c, trace)
        assert original is not None
        with pytest.raises(sqlite3.IntegrityError, match='cancel latch is immutable'):
            c.execute('UPDATE mission_tool_execution_leases '
                      'SET cancel_requested_ms=NULL WHERE trace_id=?', (trace,))
        assert latch(c, trace) == original
    finally:
        c.close()


async def test_function_committed_latch_cannot_be_rewritten(tmp_path):
    c = database(tmp_path/'m.db')
    try:
        add_mission(c); s = store(c); trace = await reserve(s)
        pause(c)
        original = latch(c, trace)
        with pytest.raises(sqlite3.IntegrityError, match='cancel latch is immutable'):
            c.execute('UPDATE mission_tool_execution_leases '
                      'SET cancel_requested_ms=? WHERE trace_id=?', (original+1, trace))
        assert latch(c, trace) == original
    finally:
        c.close()


async def test_function_first_latch_and_repeat_trigger_remain_valid(tmp_path):
    c = database(tmp_path/'m.db')
    try:
        add_mission(c); s = store(c); trace = await reserve(s)
        assert latch(c, trace) is None
        pause(c)
        original = latch(c, trace)
        assert original is not None
        # Existing V29/V30 triggers perform idempotent COALESCE updates.
        add_event(c, 'CANCELLED')
        c.execute('UPDATE mission_tool_execution_leases '
                  'SET cancel_requested_ms=cancel_requested_ms WHERE trace_id=?', (trace,))
        assert latch(c, trace) == original
    finally:
        c.close()


# INTEGRATION (3)
async def test_integration_two_connections_cannot_erase_cancel_and_dispatch(tmp_path):
    path = tmp_path/'m.db'; a = database(path); b = database(path)
    try:
        add_mission(a); s = store(a); trace = await reserve(s)
        pause(b); resume(b)
        original = latch(a, trace)
        assert original is not None
        with pytest.raises(sqlite3.IntegrityError, match='cancel latch is immutable'):
            a.execute('UPDATE mission_tool_execution_leases '
                      'SET cancel_requested_ms=NULL WHERE trace_id=?', (trace,))
        assert latch(b, trace) == original
        assert not await s.try_fence_tool_dispatch(
            trace_id=trace, mission_id='m1', tool_name='external-action')
    finally:
        a.close(); b.close()


async def test_integration_separate_os_process_cannot_clear_latch(tmp_path):
    path = tmp_path/'m.db'; c = database(path)
    try:
        add_mission(c); s = store(c); trace = await reserve(s)
        pause(c)
        original = latch(c, trace)
        code = (
            'import sqlite3,sys; '
            'c=sqlite3.connect(sys.argv[1],isolation_level=None); '
            'c.execute("UPDATE mission_tool_execution_leases '
            'SET cancel_requested_ms=NULL WHERE trace_id=?",(sys.argv[2],))'
        )
        p = subprocess.run([sys.executable, '-c', code, str(path), trace],
                           capture_output=True, text=True, timeout=8)
        assert p.returncode != 0
        assert 'cancel latch is immutable' in p.stderr
        assert latch(c, trace) == original
        assert not await s.is_execution_lease_uncancelled(
            trace_id=trace, mission_id='m1', tool_name='external-action')
    finally:
        c.close()


async def test_integration_rollback_restores_initial_latch_state(tmp_path):
    path = tmp_path/'m.db'; a = database(path); b = database(path)
    try:
        add_mission(a); s = store(a); trace = await reserve(s)
        b.execute('BEGIN IMMEDIATE')
        pause(b)
        assert latch(b, trace) is not None
        b.rollback()
        assert latch(a, trace) is None
        assert await s.try_fence_tool_dispatch(
            trace_id=trace, mission_id='m1', tool_name='external-action')
        pause(b)
        assert latch(a, trace) is not None
    finally:
        if b.in_transaction: b.rollback()
        a.close(); b.close()


# SECURITY / REGRESSION (4)
async def test_security_cannot_clear_latch_and_complete_in_one_update(tmp_path):
    c = database(tmp_path/'m.db')
    try:
        add_mission(c); s = store(c); trace = await reserve(s)
        pause(c)
        with pytest.raises(sqlite3.IntegrityError, match='cancel latch is immutable'):
            c.execute("UPDATE mission_tool_execution_leases SET outcome='completed', "
                      'cancel_requested_ms=NULL WHERE trace_id=?', (trace,))
        assert c.execute('SELECT outcome FROM mission_tool_execution_leases '
                         'WHERE trace_id=?', (trace,)).fetchone() == ('running',)
        assert await s.finish_tool_execution(trace_id=trace, outcome='completed') == 'cancelled'
    finally:
        c.close()


async def test_security_multirow_update_aborts_atomically(tmp_path):
    c = database(tmp_path/'m.db')
    try:
        add_mission(c); s = store(c)
        first = await reserve(s); second = await reserve(s)
        pause(c)
        assert latch(c, first) is not None and latch(c, second) is not None
        with pytest.raises(sqlite3.IntegrityError, match='cancel latch is immutable'):
            c.execute('UPDATE mission_tool_execution_leases SET cancel_requested_ms=NULL')
        assert latch(c, first) is not None and latch(c, second) is not None
    finally:
        c.close()


async def test_security_normal_outcome_update_preserves_latch(tmp_path):
    c = database(tmp_path/'m.db')
    try:
        add_mission(c); s = store(c); trace = await reserve(s)
        pause(c)
        original = latch(c, trace)
        assert await s.finish_tool_execution(trace_id=trace, outcome='failed') == 'failed'
        assert latch(c, trace) == original
        assert not await s.try_fence_tool_dispatch(
            trace_id=trace, mission_id='m1', tool_name='external-action')
    finally:
        c.close()


async def test_security_schema_upgrade_installs_trigger_on_existing_db(tmp_path):
    # A V43 database contains the same tables and latches, but not this trigger.
    # Reapplying V44 schema (as MissionEventStore.open does) must install it.
    from zipfile import ZipFile
    v43 = '/mnt/data/JARVIS_Dodaios_Version43_InTask_Dispatch_Cancel_Fence_2026-11-02.zip'
    with ZipFile(v43) as z:
        old_sql = z.read('Dodaios-main/jarvis/missions/missions_schema.sql').decode()
    path = tmp_path/'old.db'
    c = sqlite3.connect(path, isolation_level=None)
    try:
        c.executescript(old_sql)
        add_mission(c)
        s = store(c); trace = await reserve(s)
        pause(c)
        original = latch(c, trace)
        c.executescript(_SCHEMA_PATH.read_text(encoding='utf-8'))
        with pytest.raises(sqlite3.IntegrityError, match='cancel latch is immutable'):
            c.execute('UPDATE mission_tool_execution_leases '
                      'SET cancel_requested_ms=NULL WHERE trace_id=?', (trace,))
        assert latch(c, trace) == original
    finally:
        c.close()

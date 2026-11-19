"""V35: bounded opt-in retention with permanent non-replayable tombstones.

Production SQL on real SQLite; async stdlib adapter is only used in tests.
"""
from __future__ import annotations
import sqlite3
import subprocess
import sys
from uuid import uuid4
import pytest
from tests.v25.test_mission_execution_leases_v25 import (
    FakeTool, add_mission, config, database, setup, store,
)
pytestmark = pytest.mark.asyncio

async def claim(c):
    trace = str(uuid4())
    assert await store(c).try_start_tool_execution(trace_id=trace, mission_id='m1', tool_name='external-action')
    return trace

async def finish(c, outcome='completed'):
    trace = await claim(c)
    await store(c).finish_tool_execution(trace_id=trace, outcome=outcome)
    return trace

def tomb(c):
    return {r[0] for r in c.execute('SELECT trace_id FROM mission_tool_execution_tombstones')}

# FUNCTION (3)
async def test_function_cutoff_preserves_recent_and_running(tmp_path):
    c = database(tmp_path/'db')
    try:
        add_mission(c)
        old, running, recent = await finish(c), await claim(c), await finish(c, 'failed')
        c.execute('UPDATE mission_tool_execution_leases SET finished_ms=10 WHERE trace_id=?', (old,))
        c.execute('UPDATE mission_tool_execution_leases SET finished_ms=1000 WHERE trace_id=?', (recent,))
        assert await store(c).compact_finished_tool_executions(before_ms=100) == 1
        assert tomb(c) == {old}
        assert {r[0] for r in c.execute('SELECT trace_id FROM mission_tool_execution_leases')} == {running,recent}
    finally: c.close()

async def test_function_bounded_batches_idempotent(tmp_path):
    c = database(tmp_path/'db')
    try:
        add_mission(c); traces = {await finish(c) for _ in range(5)}; s=store(c)
        assert [await s.compact_finished_tool_executions(before_ms=10**15,limit=2) for _ in range(4)] == [2,2,1,0]
        assert tomb(c) == traces
    finally: c.close()

async def test_function_old_trace_blocked_new_trace_allowed(tmp_path):
    c=database(tmp_path/'db')
    try:
        add_mission(c); old=await finish(c)
        assert await store(c).compact_finished_tool_executions(before_ms=10**15) == 1
        assert not await store(c).try_start_tool_execution(trace_id=old,mission_id='m1',tool_name='external-action')
        assert await claim(c) != old
    finally: c.close()

# INTEGRATION (3)
async def test_integration_second_connection_replay_refused(tmp_path):
    path=tmp_path/'db'; a,b=database(path),database(path)
    try:
        add_mission(a); trace=await finish(a)
        assert await store(b).compact_finished_tool_executions(before_ms=10**15) == 1
        assert tomb(a) == {trace}
        assert not await store(a).try_start_tool_execution(trace_id=trace,mission_id='m1',tool_name='external-action')
    finally: a.close(); b.close()

async def test_integration_rollback_atomic_tombstone(tmp_path):
    path=tmp_path/'db'; a,b=database(path),database(path)
    try:
        add_mission(a); trace=await finish(a)
        b.execute('BEGIN IMMEDIATE')
        assert await store(b).compact_finished_tool_executions(before_ms=10**15) == 1
        assert tomb(b) == {trace} and tomb(a) == set()
        b.execute('ROLLBACK')
        assert tomb(a) == set()
        assert a.execute('SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?',(trace,)).fetchone() == ('completed',)
        assert await store(a).compact_finished_tool_executions(before_ms=10**15) == 1
    finally: a.close(); b.close()

async def test_integration_independent_os_process_cannot_replay(tmp_path):
    path=tmp_path/'db'; c=database(path)
    try:
        add_mission(c); trace=await finish(c)
        assert await store(c).compact_finished_tool_executions(before_ms=10**15) == 1
        code=("import sqlite3,sys; c=sqlite3.connect(sys.argv[1],isolation_level=None); "
              "c.execute(\"INSERT INTO mission_tool_execution_leases "
              "(trace_id,mission_id,tool_name,started_ms) VALUES (?, 'm1', 'external-action', 1)\", (sys.argv[2],)); "
              "print(c.execute('SELECT count(*) FROM mission_tool_execution_leases WHERE trace_id=?',"
              "(sys.argv[2],)).fetchone()[0]); c.close()")
        p=subprocess.run([sys.executable,'-c',code,str(path),trace],capture_output=True,text=True,check=True,timeout=8)
        assert p.stdout.strip() == '0'
    finally: c.close()

# SECURITY / REGRESSION (4)
async def test_security_running_cannot_be_pruned_or_directly_deleted(tmp_path):
    c=database(tmp_path/'db')
    try:
        add_mission(c); trace=await claim(c)
        assert await store(c).compact_finished_tool_executions(before_ms=10**15) == 0
        with pytest.raises(sqlite3.IntegrityError,match='running'):
            c.execute('DELETE FROM mission_tool_execution_leases WHERE trace_id=?',(trace,))
        assert c.execute('SELECT outcome FROM mission_tool_execution_leases WHERE trace_id=?',(trace,)).fetchone() == ('running',)
    finally: c.close()

async def test_security_tombstones_and_trace_ids_immutable(tmp_path):
    c=database(tmp_path/'db')
    try:
        add_mission(c); old=await finish(c); running=await claim(c)
        with pytest.raises(sqlite3.IntegrityError,match='immutable'):
            c.execute('UPDATE mission_tool_execution_leases SET trace_id=? WHERE trace_id=?',(str(uuid4()),running))
        await store(c).compact_finished_tool_executions(before_ms=10**15)
        with pytest.raises(sqlite3.IntegrityError,match='tombstones'):
            c.execute('DELETE FROM mission_tool_execution_tombstones WHERE trace_id=?',(old,))
        with pytest.raises(sqlite3.IntegrityError,match='tombstones'):
            c.execute('UPDATE mission_tool_execution_tombstones SET trace_id=? WHERE trace_id=?',(str(uuid4()),old))
        assert tomb(c) == {old}
    finally: c.close()

async def test_security_invalid_retention_arguments_fail_closed(tmp_path):
    c=database(tmp_path/'db')
    try:
        add_mission(c); trace=await finish(c); s=store(c)
        for kwargs in ({'before_ms':-1},{'before_ms':True},{'before_ms':2.5},
                       {'before_ms':10**15,'limit':0},{'before_ms':10**15,'limit':1001},
                       {'before_ms':10**15,'limit':True}):
            with pytest.raises(ValueError): await s.compact_finished_tool_executions(**kwargs)
        assert c.execute('SELECT trace_id FROM mission_tool_execution_leases').fetchone() == (trace,)
        assert tomb(c) == set()
    finally: c.close()

async def test_security_real_executor_denies_replay_after_compaction(tmp_path,monkeypatch):
    c=database(tmp_path/'db')
    try:
        add_mission(c); executor,denied=setup(monkeypatch,store(c)); tool=FakeTool(); trace=uuid4()
        assert (await executor.execute(tool,{},config_snapshot=config(),trace_id=trace)).success
        assert await store(c).compact_finished_tool_executions(before_ms=10**15) == 1
        result=await executor.execute(tool,{},config_snapshot=config(),trace_id=trace)
        assert not result.success and tool.calls == 1
        assert denied[-1].reason == 'mission_execution_claim_refused'
        assert tomb(c) == {str(trace)}
    finally: c.close()

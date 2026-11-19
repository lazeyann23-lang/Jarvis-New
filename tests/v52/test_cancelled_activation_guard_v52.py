"""V52: direct SQL cannot activate a previously cancelled reservation."""
import pathlib, sqlite3, subprocess, sys
import pytest
SCHEMA = pathlib.Path(__file__).resolve().parents[2] / 'jarvis/missions/missions_schema.sql'
@pytest.fixture
def db(tmp_path):
    path=tmp_path/'db.sqlite'; c=sqlite3.connect(path); c.executescript(SCHEMA.read_text())
    c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,cancel_requested_ms) VALUES ('cancelled','m','shell',1,2)")
    c.commit(); yield c,path; c.close()
def activation(c, trace='cancelled'):
    return c.execute('SELECT activated_ms FROM mission_tool_execution_leases WHERE trace_id=?',(trace,)).fetchone()[0]
def blocked(c,sql):
    with pytest.raises(sqlite3.IntegrityError, match='cannot activate cancelled execution'): c.execute(sql)
# Function: 3
def test_function_block_direct_activation(db):
    c,_=db; blocked(c,"UPDATE mission_tool_execution_leases SET activated_ms=3 WHERE trace_id='cancelled'"); assert activation(c) is None
def test_function_allow_uncancelled_activation(db):
    c,_=db; c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms) VALUES ('fresh','m','shell',1)"); c.execute("UPDATE mission_tool_execution_leases SET activated_ms=3 WHERE trace_id='fresh'"); assert activation(c,'fresh')==3
def test_function_allow_idempotent_null(db):
    c,_=db; c.execute("UPDATE mission_tool_execution_leases SET activated_ms=NULL WHERE trace_id='cancelled'"); assert activation(c) is None
# Integration: 3
def test_integration_other_connection(db):
    c,p=db; d=sqlite3.connect(p); blocked(d,"UPDATE mission_tool_execution_leases SET activated_ms=3 WHERE trace_id='cancelled'"); d.close(); assert activation(c) is None
def test_integration_other_process(db):
    c,p=db; code='import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute("UPDATE mission_tool_execution_leases SET activated_ms=3 WHERE trace_id=\'cancelled\'"); c.commit()'
    r=subprocess.run([sys.executable,'-c',code,str(p)],capture_output=True,text=True,timeout=10)
    assert r.returncode!=0 and 'cannot activate cancelled execution' in r.stderr; assert activation(c) is None
def test_integration_rollback(db):
    c,_=db; c.execute('SAVEPOINT a'); blocked(c,"UPDATE mission_tool_execution_leases SET activated_ms=3 WHERE trace_id='cancelled'"); c.execute('ROLLBACK TO a'); c.execute('RELEASE a'); assert activation(c) is None
# Security / regression: 4
def test_security_multirow_atomic(db):
    c,_=db; c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms) VALUES ('fresh','m','shell',1)")
    blocked(c,"UPDATE mission_tool_execution_leases SET activated_ms=3 WHERE mission_id='m'"); assert activation(c) is None and activation(c,'fresh') is None
def test_security_latch_after_activation(db):
    c,_=db; c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,activated_ms) VALUES ('fresh','m','shell',1,3)")
    c.execute("UPDATE mission_tool_execution_leases SET cancel_requested_ms=4 WHERE trace_id='fresh'"); assert activation(c,'fresh')==3
def test_security_schema_upgrade(tmp_path):
    c=sqlite3.connect(tmp_path/'old.sqlite'); s=SCHEMA.read_text(); c.executescript(s.split('-- V52:')[0]); c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,cancel_requested_ms) VALUES ('cancelled','m','shell',1,2)"); c.executescript(s); blocked(c,"UPDATE mission_tool_execution_leases SET activated_ms=3 WHERE trace_id='cancelled'"); c.close()
def test_security_existing_activation_immutable(db):
    c,_=db; c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,activated_ms) VALUES ('fresh','m','shell',1,3)")
    with pytest.raises(sqlite3.IntegrityError,match='activation latch is immutable'): c.execute("UPDATE mission_tool_execution_leases SET activated_ms=4 WHERE trace_id='fresh'")

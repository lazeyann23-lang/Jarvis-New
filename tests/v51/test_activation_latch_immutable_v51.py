"""V51: a committed execution activation timestamp cannot be rewritten."""
import pathlib, sqlite3, subprocess, sys
import pytest
SCHEMA = pathlib.Path(__file__).resolve().parents[2] / 'jarvis/missions/missions_schema.sql'
@pytest.fixture
def db(tmp_path):
    p=tmp_path/'m.db'; c=sqlite3.connect(p); c.executescript(SCHEMA.read_text())
    c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,activated_ms) VALUES ('a','m','shell',1,2)")
    c.commit(); yield c,p; c.close()
def activation(c):
    return c.execute("SELECT activated_ms FROM mission_tool_execution_leases WHERE trace_id='a'").fetchone()[0]
def reject(c,sql):
    with pytest.raises(sqlite3.IntegrityError,match='activation latch is immutable'): c.execute(sql)
# Function (3)
def test_function_cannot_clear(db):
    c,_=db; reject(c,"UPDATE mission_tool_execution_leases SET activated_ms=NULL WHERE trace_id='a'"); assert activation(c)==2
def test_function_cannot_rewrite(db):
    c,_=db; reject(c,"UPDATE mission_tool_execution_leases SET activated_ms=9 WHERE trace_id='a'"); assert activation(c)==2
def test_function_idempotent(db):
    c,_=db; c.execute("UPDATE mission_tool_execution_leases SET activated_ms=2 WHERE trace_id='a'"); assert activation(c)==2
# Integration (3)
def test_integration_second_connection(db):
    c,p=db; d=sqlite3.connect(p); reject(d,"UPDATE mission_tool_execution_leases SET activated_ms=7 WHERE trace_id='a'"); d.close(); assert activation(c)==2
def test_integration_separate_process(db):
    c,p=db; code="import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute(\"UPDATE mission_tool_execution_leases SET activated_ms=7 WHERE trace_id='a'\"); c.commit()"
    r=subprocess.run([sys.executable,'-c',code,str(p)],capture_output=True,text=True,timeout=10)
    assert r.returncode!=0 and 'activation latch is immutable' in r.stderr; assert activation(c)==2
def test_integration_rollback(db):
    c,_=db; c.execute('SAVEPOINT x'); reject(c,"UPDATE mission_tool_execution_leases SET activated_ms=7 WHERE trace_id='a'"); c.execute('ROLLBACK TO x'); c.execute('RELEASE x'); assert activation(c)==2
# Security / regression (4)
def test_security_multirow_atomic(db):
    c,_=db; c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms) VALUES ('b','m','shell',3)")
    reject(c,"UPDATE mission_tool_execution_leases SET activated_ms=10 WHERE mission_id='m'")
    assert activation(c)==2 and c.execute("SELECT activated_ms FROM mission_tool_execution_leases WHERE trace_id='b'").fetchone()[0] is None
def test_security_first_activation(db):
    c,_=db; c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms) VALUES ('b','m','shell',3)")
    c.execute("UPDATE mission_tool_execution_leases SET activated_ms=4 WHERE trace_id='b'")
    assert c.execute("SELECT activated_ms FROM mission_tool_execution_leases WHERE trace_id='b'").fetchone()[0]==4
def test_security_terminal_lifecycle(db):
    c,_=db; c.execute("UPDATE mission_tool_execution_leases SET finished_ms=5,outcome='completed' WHERE trace_id='a'")
    assert c.execute("SELECT outcome,activated_ms FROM mission_tool_execution_leases WHERE trace_id='a'").fetchone()==('completed',2)
def test_security_schema_upgrade(tmp_path):
    c=sqlite3.connect(tmp_path/'old.db'); s=SCHEMA.read_text(); c.executescript(s.split('-- V51:')[0]);
    c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,activated_ms) VALUES ('a','m','shell',1,2)")
    c.executescript(s); reject(c,"UPDATE mission_tool_execution_leases SET activated_ms=NULL WHERE trace_id='a'"); c.close()

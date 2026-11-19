"""V53: terminal execution leases must never be newly activated."""
import pathlib, sqlite3, subprocess, sys
import pytest
SCHEMA=pathlib.Path(__file__).resolve().parents[2]/'jarvis/missions/missions_schema.sql'
@pytest.fixture
def db(tmp_path):
 p=tmp_path/'test.sqlite'; c=sqlite3.connect(p); c.executescript(SCHEMA.read_text()); c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,finished_ms,outcome) VALUES ('done','m','shell',1,2,'completed')"); c.commit(); yield c,p; c.close()
def reject(c,where="trace_id='done'"):
 with pytest.raises(sqlite3.IntegrityError,match='cannot activate finalized execution'):
  c.execute('UPDATE mission_tool_execution_leases SET activated_ms=3 WHERE '+where)
def activation(c,trace='done'):
 return c.execute('SELECT activated_ms FROM mission_tool_execution_leases WHERE trace_id=?',(trace,)).fetchone()[0]
# Function (3)
def test_function_completed_rejected(db):
 c,_=db;reject(c);assert activation(c) is None
def test_function_failed_rejected(db):
 c,_=db;c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,finished_ms,outcome) VALUES ('fail','m','shell',1,2,'failed')");reject(c,"trace_id='fail'")
def test_function_running_allowed(db):
 c,_=db;c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms) VALUES ('run','m','shell',1)");c.execute("UPDATE mission_tool_execution_leases SET activated_ms=3 WHERE trace_id='run'");assert activation(c,'run')==3
# Integration (3)
def test_integration_other_connection(db):
 c,p=db;d=sqlite3.connect(p);reject(d);d.close();assert activation(c) is None
def test_integration_other_process(db):
 c,p=db;code='import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute("UPDATE mission_tool_execution_leases SET activated_ms=3 WHERE trace_id=\'done\'"); c.commit()';r=subprocess.run([sys.executable,'-c',code,str(p)],capture_output=True,text=True,timeout=10);assert r.returncode!=0 and 'cannot activate finalized execution' in r.stderr
def test_integration_savepoint(db):
 c,_=db;c.execute('SAVEPOINT a');reject(c);c.execute('ROLLBACK TO a');c.execute('RELEASE a');assert activation(c) is None
# Security/regression (4)
def test_security_multirow_atomic(db):
 c,_=db;c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms) VALUES ('run','m','shell',1)");reject(c,"mission_id='m'");assert activation(c,'run') is None
def test_security_cancelled_terminal_rejected(db):
 c,_=db;c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,finished_ms,outcome) VALUES ('cancel','m','shell',1,2,'cancelled')");reject(c,"trace_id='cancel'")
def test_security_existing_activation_idempotent(db):
 c,_=db;c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,activated_ms,finished_ms,outcome) VALUES ('old','m','shell',1,3,4,'completed')");c.execute("UPDATE mission_tool_execution_leases SET activated_ms=3 WHERE trace_id='old'");assert activation(c,'old')==3
def test_security_schema_upgrade(tmp_path):
 s=SCHEMA.read_text();c=sqlite3.connect(tmp_path/'old.sqlite');c.executescript(s.split('-- V53:')[0]);c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,finished_ms,outcome) VALUES ('done','m','shell',1,2,'completed')");c.executescript(s);reject(c);c.close()

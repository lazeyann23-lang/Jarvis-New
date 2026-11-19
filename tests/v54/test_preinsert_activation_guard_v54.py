"""V54: prevent direct INSERT bypass of stopped-execution activation fences."""
import pathlib, sqlite3, subprocess, sys
import pytest
SCHEMA=pathlib.Path(__file__).resolve().parents[2]/'jarvis/missions/missions_schema.sql'
@pytest.fixture
def db(tmp_path):
 p=tmp_path/'v54.sqlite';c=sqlite3.connect(p);c.executescript(SCHEMA.read_text());yield c,p;c.close()
def insert(c,trace='bad',activated=3,cancel=None,outcome='running'):
 return c.execute('INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,activated_ms,cancel_requested_ms,outcome) VALUES (?,?,?,?,?,?,?)',(trace,'m','shell',1,activated,cancel,outcome))
def rejected(c,**kw):
 with pytest.raises(sqlite3.IntegrityError,match='cannot insert preactivated stopped execution'):insert(c,**kw)
def absent(c,trace='bad'):
 assert c.execute('SELECT count(*) FROM mission_tool_execution_leases WHERE trace_id=?',(trace,)).fetchone()[0]==0
# Function (3)
def test_function_cancelled_latch(db):
 c,_=db;rejected(c,cancel=2);absent(c)
def test_function_final_outcome(db):
 c,_=db;rejected(c,outcome='completed');absent(c)
def test_function_running_preapproved_legacy_compatible(db):
 c,_=db;insert(c);assert c.execute("SELECT activated_ms FROM mission_tool_execution_leases WHERE trace_id='bad'").fetchone()[0]==3
# Integration (3)
def test_integration_second_connection(db):
 c,p=db;c.commit();d=sqlite3.connect(p);rejected(d,cancel=2);d.close();absent(c)
def test_integration_separate_process(db):
 c,p=db;c.commit()
 code='import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,activated_ms,outcome) VALUES (?,?,?,?,?,?)",("bad","m","shell",1,3,"failed"))'
 r=subprocess.run([sys.executable,'-c',code,str(p)],capture_output=True,text=True,timeout=10)
 assert r.returncode!=0 and 'cannot insert preactivated stopped execution' in r.stderr
 absent(c)
def test_integration_rollback(db):
 c,_=db;c.execute('SAVEPOINT s');rejected(c,cancel=2);c.execute('ROLLBACK TO s');c.execute('RELEASE s');absent(c)
# Security/regression (4)
def test_security_cancelled_terminal_combined(db):
 c,_=db;rejected(c,cancel=2,outcome='cancelled');absent(c)
def test_security_multirow_atomic(db):
 c,_=db
 with pytest.raises(sqlite3.IntegrityError,match='cannot insert preactivated stopped execution'):
  c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,activated_ms,cancel_requested_ms) VALUES ('good','m','shell',1,3,NULL),('bad','m','shell',1,3,2)")
 absent(c,'good');absent(c)
def test_security_normal_claim_and_later_activation(db):
 c,_=db;insert(c,activated=None,cancel=None);c.execute("UPDATE mission_tool_execution_leases SET activated_ms=3 WHERE trace_id='bad'");assert c.execute("SELECT activated_ms FROM mission_tool_execution_leases WHERE trace_id='bad'").fetchone()[0]==3
def test_security_schema_upgrade(db):
 c,_=db;c.executescript(SCHEMA.read_text());rejected(c,outcome='failed');absent(c)

"""V50: finalized execution leases cannot be reopened or rewritten."""
import pathlib,sqlite3,subprocess,sys,pytest
SCHEMA=pathlib.Path(__file__).resolve().parents[2]/'jarvis/missions/missions_schema.sql'
@pytest.fixture
def db(tmp_path):
 p=tmp_path/'mission.db'; c=sqlite3.connect(p);c.executescript(SCHEMA.read_text());c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,finished_ms,outcome) VALUES ('t','m','shell',1,2,'completed')");c.commit();yield c,p;c.close()
def state(c):return c.execute("SELECT outcome,finished_ms FROM mission_tool_execution_leases WHERE trace_id='t'").fetchone()
def rejected(c,sql):
 with pytest.raises(sqlite3.IntegrityError):c.execute(sql)
# Function: 3
def test_function_no_reopen(db):
 c,_=db;rejected(c,"UPDATE mission_tool_execution_leases SET outcome='running' WHERE trace_id='t'");assert state(c)==('completed',2)
def test_function_no_terminal_rewrite(db):
 c,_=db;rejected(c,"UPDATE mission_tool_execution_leases SET outcome='failed' WHERE trace_id='t'");assert state(c)==('completed',2)
def test_function_no_finished_timestamp_rewrite(db):
 c,_=db;rejected(c,"UPDATE mission_tool_execution_leases SET finished_ms=999 WHERE trace_id='t'");assert state(c)==('completed',2)
# Integration: 3
def test_integration_second_connection(db):
 c,p=db;d=sqlite3.connect(p);rejected(d,"UPDATE mission_tool_execution_leases SET outcome='running' WHERE trace_id='t'");d.close();assert state(c)==('completed',2)
def test_integration_separate_os_process(db):
 c,p=db;sql="UPDATE mission_tool_execution_leases SET outcome='running' WHERE trace_id='t'";code='import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('+repr(sql)+'); c.commit()';r=subprocess.run([sys.executable,'-c',code,str(p)],capture_output=True,text=True,timeout=10);assert r.returncode!=0 and 'finalized execution outcome is immutable' in r.stderr;assert state(c)==('completed',2)
def test_integration_transaction_rollback(db):
 c,_=db;c.execute('SAVEPOINT test');rejected(c,"UPDATE mission_tool_execution_leases SET outcome='running' WHERE trace_id='t'");c.execute('ROLLBACK TO test');c.execute('RELEASE test');assert state(c)==('completed',2)
# Security/regression: 4
def test_security_multirow_atomic(db):
 c,_=db;c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,outcome) VALUES ('r','m','shell',3,'running')");rejected(c,"UPDATE mission_tool_execution_leases SET outcome='running' WHERE mission_id='m'");assert state(c)==('completed',2)
def test_security_idempotent_final_update(db):
 c,_=db;c.execute("UPDATE mission_tool_execution_leases SET outcome='completed',finished_ms=2 WHERE trace_id='t'");assert state(c)==('completed',2)
def test_security_normal_finalization(db):
 c,_=db;c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,outcome) VALUES ('r','m','shell',3,'running')");c.execute("UPDATE mission_tool_execution_leases SET outcome='cancelled',finished_ms=4 WHERE trace_id='r'");assert c.execute("SELECT outcome,finished_ms FROM mission_tool_execution_leases WHERE trace_id='r'").fetchone()==('cancelled',4)
def test_security_existing_db_schema_upgrade(tmp_path):
 p=tmp_path/'old.db';c=sqlite3.connect(p);s=SCHEMA.read_text();c.executescript(s.split('-- V50:')[0]);c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,finished_ms,outcome) VALUES ('t','m','shell',1,2,'completed')");c.executescript(s);rejected(c,"UPDATE mission_tool_execution_leases SET outcome='running' WHERE trace_id='t'");c.close()

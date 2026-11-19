"""V48: execution reservation identity must never be rebound by ordinary SQL."""
import pathlib
import sqlite3
import subprocess
import sys
import pytest

SCHEMA = pathlib.Path(__file__).resolve().parents[2] / 'jarvis/missions/missions_schema.sql'

@pytest.fixture
def db(tmp_path):
    path = tmp_path / 'missions.sqlite'
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text())
    con.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms) VALUES ('t1','m1','shell',100)")
    con.commit()
    yield con, path
    con.close()

# Function (3)
@pytest.mark.parametrize('column,value', [('mission_id','m2'),('tool_name','python'),('started_ms',200)])
def test_function_identity_cannot_change(db,column,value):
    con,_=db
    with pytest.raises(sqlite3.IntegrityError,match='identity is immutable'):
        con.execute(f'UPDATE mission_tool_execution_leases SET {column}=? WHERE trace_id=?',(value,'t1'))

# Integration (3)
def test_integration_second_connection(db):
    con,path=db
    other=sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        other.execute("UPDATE mission_tool_execution_leases SET mission_id='m2' WHERE trace_id='t1'")
    assert con.execute("SELECT mission_id FROM mission_tool_execution_leases WHERE trace_id='t1'").fetchone()==('m1',)
    other.close()

def test_integration_separate_process(db):
    con,path=db
    code="import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute(\"UPDATE mission_tool_execution_leases SET tool_name='other' WHERE trace_id='t1'\")"
    proc=subprocess.run([sys.executable,'-c',code,str(path)],capture_output=True,text=True,timeout=10)
    assert proc.returncode!=0 and 'identity is immutable' in proc.stderr
    assert con.execute("SELECT tool_name FROM mission_tool_execution_leases WHERE trace_id='t1'").fetchone()==('shell',)

def test_integration_multirow_atomicity(db):
    con,_=db
    con.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms) VALUES ('t2','m2','shell',200)")
    con.commit()
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE mission_tool_execution_leases SET mission_id='different'")
    assert con.execute('SELECT mission_id FROM mission_tool_execution_leases ORDER BY trace_id').fetchall()==[('m1',),('m2',)]

# Security/regression (4)
def test_security_idempotent_identity_update(db):
    con,_=db
    con.execute("UPDATE mission_tool_execution_leases SET mission_id='m1',tool_name='shell',started_ms=100 WHERE trace_id='t1'")
    assert con.execute("SELECT mission_id FROM mission_tool_execution_leases WHERE trace_id='t1'").fetchone()==('m1',)

def test_security_lifecycle_update_still_allowed(db):
    con,_=db
    con.execute("UPDATE mission_tool_execution_leases SET activated_ms=101 WHERE trace_id='t1'")
    con.execute("UPDATE mission_tool_execution_leases SET outcome='cancelled',finished_ms=102 WHERE trace_id='t1'")
    assert con.execute("SELECT outcome FROM mission_tool_execution_leases WHERE trace_id='t1'").fetchone()==('cancelled',)

def test_security_replace_guard_preserved(db):
    con,_=db
    con.execute("INSERT OR REPLACE INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms) VALUES ('t1','evil','other',200)")
    assert con.execute("SELECT mission_id FROM mission_tool_execution_leases WHERE trace_id='t1'").fetchone()==('m1',)

def test_security_schema_upgrade(tmp_path):
    con=sqlite3.connect(tmp_path/'old.sqlite')
    schema=SCHEMA.read_text()
    marker='-- V48: a reservation is bound to its original mission, tool and start time.'
    block=schema[schema.index(marker):schema.index('-- V44: once a lifecycle/header cancellation')]
    con.executescript(schema.replace(block,''))
    con.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms) VALUES ('t1','m1','shell',100)")
    con.executescript(schema)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE mission_tool_execution_leases SET tool_name='other' WHERE trace_id='t1'")
    con.close()

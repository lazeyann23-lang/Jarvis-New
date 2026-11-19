import pathlib, sqlite3, subprocess, sys, tempfile
import pytest

SCHEMA = pathlib.Path(__file__).resolve().parents[2] / 'jarvis' / 'missions' / 'missions_schema.sql'

@pytest.fixture
def db(tmp_path):
    path=tmp_path/'mission.sqlite'
    con=sqlite3.connect(path)
    con.executescript(SCHEMA.read_text())
    con.execute("INSERT INTO mission_tool_approval_decisions VALUES ('trace-1','mission-1','shell','denied',100)")
    con.commit()
    yield con,path
    con.close()

# Function (3)
def test_function_denial_cannot_be_approved(db):
    c,_=db
    with pytest.raises(sqlite3.IntegrityError):c.execute("UPDATE mission_tool_approval_decisions SET decision='approved' WHERE trace_id='trace-1'")
    assert c.execute("SELECT decision FROM mission_tool_approval_decisions").fetchone()==('denied',)

def test_function_metadata_immutable(db):
    c,_=db
    for field,value in [('mission_id','other'),('tool_name','python'),('claimed_ms',200)]:
        with pytest.raises(sqlite3.IntegrityError):c.execute(f'UPDATE mission_tool_approval_decisions SET {field}=? WHERE trace_id=?',(value,'trace-1'))

def test_function_idempotent_and_new_claim(db):
    c,_=db
    c.execute("UPDATE mission_tool_approval_decisions SET decision='denied' WHERE trace_id='trace-1'")
    c.execute("INSERT INTO mission_tool_approval_decisions VALUES ('trace-2','mission-1','shell','approved',200)")
    assert c.execute('SELECT COUNT(*) FROM mission_tool_approval_decisions').fetchone()[0]==2

# Integration (3)
def test_integration_second_connection(db):
    c,p=db
    other=sqlite3.connect(p)
    with pytest.raises(sqlite3.IntegrityError):other.execute("UPDATE mission_tool_approval_decisions SET decision='approved' WHERE trace_id='trace-1'")
    assert c.execute("SELECT decision FROM mission_tool_approval_decisions WHERE trace_id='trace-1'").fetchone()==('denied',)
    other.close()

def test_integration_external_process(db):
    c,p=db
    script="import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute(\"UPDATE mission_tool_approval_decisions SET decision='approved' WHERE trace_id='trace-1'\")"
    r=subprocess.run([sys.executable,'-c',script,str(p)],capture_output=True,text=True,timeout=10)
    assert r.returncode!=0 and 'immutable' in r.stderr
    assert c.execute('SELECT decision FROM mission_tool_approval_decisions').fetchone()==('denied',)

def test_integration_rollback_multirow(db):
    c,_=db
    c.execute("INSERT INTO mission_tool_approval_decisions VALUES ('trace-2','mission-1','shell','approved',200)")
    c.commit()
    with pytest.raises(sqlite3.IntegrityError):c.execute("UPDATE mission_tool_approval_decisions SET decision='approved'")
    assert c.execute("SELECT decision FROM mission_tool_approval_decisions WHERE trace_id='trace-1'").fetchone()==('denied',)

# Security / regression (4)
def test_security_replace_guard_still_active(db):
    c,_=db
    c.execute("INSERT OR REPLACE INTO mission_tool_approval_decisions VALUES ('trace-1','mission-1','shell','approved',200)")
    assert c.execute('SELECT decision FROM mission_tool_approval_decisions WHERE trace_id=\'trace-1\'').fetchone()==('denied',)

def test_security_trace_id_immutable(db):
    c,_=db
    with pytest.raises(sqlite3.IntegrityError):c.execute("UPDATE mission_tool_approval_decisions SET trace_id='changed' WHERE trace_id='trace-1'")

def test_security_existing_database_upgrade(tmp_path):
    c=sqlite3.connect(tmp_path/'upgrade.db')
    full=SCHEMA.read_text()
    marker='-- V47: approval decisions are one-shot audit records.'
    block=full[full.index(marker):full.index('CREATE TRIGGER IF NOT EXISTS trg_mission_approval_trace_immutable')] if marker in full else ''
    c.executescript(full.replace(block,''))
    c.execute("INSERT INTO mission_tool_approval_decisions VALUES ('t','m','shell','denied',1)")
    c.executescript(full)
    with pytest.raises(sqlite3.IntegrityError):c.execute("UPDATE mission_tool_approval_decisions SET decision='approved' WHERE trace_id='t'")
    c.close()

def test_security_tombstone_replay(db):
    c,_=db
    c.execute("DELETE FROM mission_tool_approval_decisions WHERE trace_id='trace-1'")
    c.execute("INSERT INTO mission_tool_approval_decisions VALUES ('trace-1','mission-1','shell','approved',200)")
    assert c.execute("SELECT COUNT(*) FROM mission_tool_approval_decisions WHERE trace_id='trace-1'").fetchone()[0]==0

"""V49: compacted trace tombstones resist SQLite REPLACE replay/metadata resets."""
import pathlib
import sqlite3
import subprocess
import sys
import pytest

SCHEMA = pathlib.Path(__file__).resolve().parents[2] / 'jarvis/missions/missions_schema.sql'
TABLES = ('mission_tool_approval_tombstones', 'mission_tool_execution_tombstones')

@pytest.fixture
def db(tmp_path):
    path = tmp_path / 'missions.db'
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.read_text())
    for table in TABLES:
        con.execute(f"INSERT INTO {table}(trace_id,compacted_ms) VALUES ('trace',100)")
    con.commit()
    yield con, path
    con.close()

# Function (3)
@pytest.mark.parametrize('table,sql', [
    (TABLES[0], 'INSERT OR REPLACE'),
    (TABLES[1], 'INSERT OR REPLACE'),
    (TABLES[0], 'REPLACE'),
])
def test_function_duplicate_replace_preserves_original(db,table,sql):
    con,_=db
    con.execute(f"{sql} INTO {table}(trace_id,compacted_ms) VALUES ('trace',999)")
    assert con.execute(f"SELECT compacted_ms FROM {table} WHERE trace_id='trace'").fetchone()==(100,)

# Integration (3)
def test_integration_second_connection(db):
    con,path=db
    other=sqlite3.connect(path)
    other.execute(f"REPLACE INTO {TABLES[1]} VALUES ('trace',999)")
    other.commit()
    assert con.execute(f"SELECT compacted_ms FROM {TABLES[1]} WHERE trace_id='trace'").fetchone()==(100,)
    other.close()

def test_integration_separate_process(db):
    con,path=db
    code=f"import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute(\"REPLACE INTO {TABLES[0]} VALUES ('trace',999)\"); c.commit()"
    proc=subprocess.run([sys.executable,'-c',code,str(path)],capture_output=True,text=True,timeout=10)
    assert proc.returncode==0,proc.stderr
    assert con.execute(f"SELECT compacted_ms FROM {TABLES[0]} WHERE trace_id='trace'").fetchone()==(100,)

def test_integration_rollback(db):
    con,_=db
    con.execute('SAVEPOINT test')
    con.execute(f"INSERT OR REPLACE INTO {TABLES[0]} VALUES ('trace',999)")
    con.execute('ROLLBACK TO test')
    con.execute('RELEASE test')
    assert con.execute(f"SELECT compacted_ms FROM {TABLES[0]} WHERE trace_id='trace'").fetchone()==(100,)

# Security/regression (4)
def test_security_replay_blocked(db):
    con,_=db
    con.execute("INSERT OR IGNORE INTO mission_tool_approval_decisions VALUES ('trace','m','shell','approved',123)")
    con.execute("INSERT OR IGNORE INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms) VALUES ('trace','m','shell',123)")
    assert con.execute("SELECT count(*) FROM mission_tool_approval_decisions").fetchone()==(0,)
    assert con.execute("SELECT count(*) FROM mission_tool_execution_leases").fetchone()==(0,)

def test_security_fresh_tombstones(db):
    con,_=db
    for table in TABLES:
        con.execute(f"INSERT INTO {table} VALUES ('fresh',222)")
        assert con.execute(f"SELECT compacted_ms FROM {table} WHERE trace_id='fresh'").fetchone()==(222,)

def test_security_direct_updates_still_denied(db):
    con,_=db
    for table in TABLES:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(f"UPDATE {table} SET compacted_ms=999 WHERE trace_id='trace'")

def test_security_upgrade_existing_db(tmp_path):
    schema=SCHEMA.read_text()
    marker='-- V49: retained trace tombstones are permanent replay barriers.'
    con=sqlite3.connect(tmp_path/'old.db')
    con.executescript(schema[:schema.index(marker)])
    for table in TABLES:
        con.execute(f"INSERT INTO {table} VALUES ('trace',100)")
    con.executescript(schema)
    for table in TABLES:
        con.execute(f"REPLACE INTO {table} VALUES ('trace',999)")
        assert con.execute(f"SELECT compacted_ms FROM {table} WHERE trace_id='trace'").fetchone()==(100,)
    con.close()

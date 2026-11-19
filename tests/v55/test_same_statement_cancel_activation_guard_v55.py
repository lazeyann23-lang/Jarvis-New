import sqlite3
import pathlib
import tempfile
import multiprocessing
import pytest

SCHEMA = (pathlib.Path(__file__).resolve().parents[2] / "jarvis/missions/missions_schema.sql").read_text()

def db():
    c=sqlite3.connect(":memory:")
    c.executescript(SCHEMA)
    c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms) VALUES ('t','m','tool',1)")
    return c

def test_function_simultaneous_cancel_activation_rejected():
    c=db()
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE mission_tool_execution_leases SET activated_ms=10,cancel_requested_ms=11 WHERE trace_id='t'")
    assert c.execute("SELECT activated_ms,cancel_requested_ms FROM mission_tool_execution_leases").fetchone()==(None,None)

def test_function_cancel_first_then_activation_rejected():
    c=db();c.execute("UPDATE mission_tool_execution_leases SET cancel_requested_ms=11 WHERE trace_id='t'")
    with pytest.raises(sqlite3.IntegrityError):c.execute("UPDATE mission_tool_execution_leases SET activated_ms=10 WHERE trace_id='t'")

def test_function_clean_activation_allowed():
    c=db();c.execute("UPDATE mission_tool_execution_leases SET activated_ms=10 WHERE trace_id='t'")
    assert c.execute("SELECT activated_ms FROM mission_tool_execution_leases").fetchone()[0]==10

def filedb():
    t=tempfile.TemporaryDirectory();p=str(pathlib.Path(t.name)/"x.db")
    c=sqlite3.connect(p);c.executescript(SCHEMA);c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms) VALUES ('t','m','tool',1)");c.commit();c.close()
    return t,p

def test_integration_two_connections_rejected():
    t,p=filedb()
    try:
        a=sqlite3.connect(p);b=sqlite3.connect(p)
        with pytest.raises(sqlite3.IntegrityError):a.execute("UPDATE mission_tool_execution_leases SET activated_ms=10,cancel_requested_ms=11 WHERE trace_id='t'")
        assert b.execute("SELECT activated_ms,cancel_requested_ms FROM mission_tool_execution_leases").fetchone()==(None,None)
        a.close();b.close()
    finally:t.cleanup()

def test_integration_rollback_after_rejection():
    t,p=filedb()
    try:
        a=sqlite3.connect(p);a.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):a.execute("UPDATE mission_tool_execution_leases SET activated_ms=10,cancel_requested_ms=11 WHERE trace_id='t'")
        a.rollback();assert sqlite3.connect(p).execute("SELECT activated_ms FROM mission_tool_execution_leases").fetchone()[0] is None
        a.close()
    finally:t.cleanup()

def _writer(p,q):
    c=sqlite3.connect(p)
    try:c.execute("UPDATE mission_tool_execution_leases SET activated_ms=10,cancel_requested_ms=11 WHERE trace_id='t'");c.commit();q.put('accepted')
    except sqlite3.IntegrityError:q.put('rejected')
    finally:c.close()

def test_integration_separate_process():
    t,p=filedb()
    try:
        q=multiprocessing.Queue();proc=multiprocessing.Process(target=_writer,args=(p,q));proc.start();proc.join(10)
        assert proc.exitcode==0 and q.get(timeout=2)=='rejected'
    finally:t.cleanup()

def test_security_reversed_set_order():
    c=db()
    with pytest.raises(sqlite3.IntegrityError):c.execute("UPDATE mission_tool_execution_leases SET cancel_requested_ms=11,activated_ms=10 WHERE trace_id='t'")

def test_security_multirow_atomicity():
    c=db();c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms) VALUES ('u','m','tool',2)")
    with pytest.raises(sqlite3.IntegrityError):c.execute("UPDATE mission_tool_execution_leases SET activated_ms=10,cancel_requested_ms=11")
    assert c.execute("SELECT count(*) FROM mission_tool_execution_leases WHERE activated_ms IS NOT NULL").fetchone()[0]==0

def test_security_existing_activation_cancellation_allowed():
    c=db();c.execute("UPDATE mission_tool_execution_leases SET activated_ms=10 WHERE trace_id='t'")
    c.execute("UPDATE mission_tool_execution_leases SET cancel_requested_ms=11 WHERE trace_id='t'")
    assert c.execute("SELECT activated_ms,cancel_requested_ms FROM mission_tool_execution_leases").fetchone()==(10,11)

def test_security_no_op_update_allowed():
    c=db();c.execute("UPDATE mission_tool_execution_leases SET activated_ms=NULL,cancel_requested_ms=NULL WHERE trace_id='t'")
    assert c.execute("SELECT count(*) FROM mission_tool_execution_leases").fetchone()[0]==1

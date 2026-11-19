import sqlite3, pathlib, tempfile, multiprocessing, pytest
SCHEMA = (pathlib.Path(__file__).resolve().parents[2] / "jarvis/missions/missions_schema.sql").read_text()

def setup(path=":memory:"):
    c=sqlite3.connect(path,timeout=5)
    c.executescript(SCHEMA)
    c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms) VALUES ('t','m','tool',1)")
    c.commit()
    return c

# FUNCTION (3)
def test_function_same_statement_terminal_activation_blocked():
    c=setup()
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE mission_tool_execution_leases SET activated_ms=5,outcome='completed',finished_ms=6 WHERE trace_id='t'")
    assert c.execute("SELECT activated_ms,outcome FROM mission_tool_execution_leases").fetchone()==(None,'running')
def test_function_terminal_first_blocked():
    c=setup();c.execute("UPDATE mission_tool_execution_leases SET outcome='completed',finished_ms=6")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE mission_tool_execution_leases SET activated_ms=5")
def test_function_clean_activation_then_completion():
    c=setup();c.execute("UPDATE mission_tool_execution_leases SET activated_ms=5")
    c.execute("UPDATE mission_tool_execution_leases SET outcome='completed',finished_ms=6")
    assert c.execute("SELECT activated_ms,outcome FROM mission_tool_execution_leases").fetchone()==(5,'completed')

# INTEGRATION (3)
def _file():
    d=tempfile.TemporaryDirectory();p=str(pathlib.Path(d.name)/'db.sqlite')
    c=setup(p);c.close()
    return d,p
def test_integration_two_connections():
    d,p=_file()
    try:
        a=sqlite3.connect(p);b=sqlite3.connect(p)
        with pytest.raises(sqlite3.IntegrityError):
            a.execute("UPDATE mission_tool_execution_leases SET activated_ms=5,outcome='failed',finished_ms=6")
        assert b.execute("SELECT activated_ms,outcome FROM mission_tool_execution_leases").fetchone()==(None,'running')
        a.close();b.close()
    finally:d.cleanup()
def test_integration_transaction_rollback():
    d,p=_file()
    try:
        a=sqlite3.connect(p);a.execute("BEGIN")
        with pytest.raises(sqlite3.IntegrityError):
            a.execute("UPDATE mission_tool_execution_leases SET activated_ms=5,outcome='cancelled',finished_ms=6")
        a.rollback()
        assert sqlite3.connect(p).execute("SELECT outcome FROM mission_tool_execution_leases").fetchone()[0]=='running'
        a.close()
    finally:d.cleanup()
def _worker(p,q):
    c=sqlite3.connect(p)
    try:
        c.execute("UPDATE mission_tool_execution_leases SET activated_ms=5,outcome='completed',finished_ms=6")
        c.commit();q.put('accepted')
    except sqlite3.IntegrityError:q.put('rejected')
    finally:c.close()
def test_integration_other_process():
    d,p=_file()
    try:
        q=multiprocessing.Queue();pr=multiprocessing.Process(target=_worker,args=(p,q))
        pr.start();pr.join(10)
        assert pr.exitcode==0 and q.get(timeout=2)=='rejected'
    finally:d.cleanup()

# SECURITY / REGRESSION (4)
def test_security_reversed_columns():
    c=setup()
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE mission_tool_execution_leases SET outcome='completed',finished_ms=6,activated_ms=5")
def test_security_multirow_statement_rollback():
    c=setup();c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms) VALUES ('u','m','tool',2)")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE mission_tool_execution_leases SET activated_ms=5,outcome='completed',finished_ms=6")
    assert c.execute("SELECT count(*) FROM mission_tool_execution_leases WHERE activated_ms IS NOT NULL").fetchone()[0]==0
def test_security_existing_activation_can_finish():
    c=setup();c.execute("UPDATE mission_tool_execution_leases SET activated_ms=5")
    c.execute("UPDATE mission_tool_execution_leases SET outcome='failed',finished_ms=6")
    assert c.execute("SELECT outcome FROM mission_tool_execution_leases").fetchone()[0]=='failed'
def test_security_noop_activation_update():
    c=setup();c.execute("UPDATE mission_tool_execution_leases SET activated_ms=NULL,outcome='running'")
    assert c.execute("SELECT count(*) FROM mission_tool_execution_leases").fetchone()[0]==1

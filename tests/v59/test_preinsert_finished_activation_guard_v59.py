import sqlite3, tempfile, subprocess, sys, unittest
from pathlib import Path
SCHEMA = Path(__file__).resolve().parents[2] / 'jarvis/missions/missions_schema.sql'

def db():
    c=sqlite3.connect(':memory:'); c.executescript(SCHEMA.read_text()); return c
def insert(c, trace, activated=None, finished=None, outcome='running'):
    c.execute('INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,activated_ms,finished_ms,outcome) VALUES(?,?,?,?,?,?,?)', (trace,'m','tool',1,activated,finished,outcome))
class Function(unittest.TestCase):
    def test_reject_both(self):
        c=db()
        with self.assertRaises(sqlite3.IntegrityError): insert(c,'a',2,3)
    def test_unactivated_finished_allowed(self):
        c=db(); insert(c,'b',None,3); self.assertEqual(c.execute('select finished_ms from mission_tool_execution_leases').fetchone()[0],3)
    def test_activated_running_allowed(self):
        c=db(); insert(c,'c',2,None); self.assertEqual(c.execute('select activated_ms from mission_tool_execution_leases').fetchone()[0],2)
class Integration(unittest.TestCase):
    def test_transaction_rollback(self):
        c=db(); c.execute('SAVEPOINT s')
        with self.assertRaises(sqlite3.IntegrityError): insert(c,'d',2,3)
        c.execute('ROLLBACK TO s'); insert(c,'d',None,None); self.assertEqual(c.execute('select count(*) from mission_tool_execution_leases').fetchone()[0],1)
    def test_two_connections(self):
        with tempfile.TemporaryDirectory() as d:
            p=str(Path(d)/'x.db'); a=sqlite3.connect(p); a.executescript(SCHEMA.read_text()); a.commit(); b=sqlite3.connect(p)
            with self.assertRaises(sqlite3.IntegrityError): insert(b,'e',2,3)
            self.assertEqual(a.execute('select count(*) from mission_tool_execution_leases').fetchone()[0],0); b.close(); a.close()
    def test_other_process(self):
        with tempfile.TemporaryDirectory() as d:
            p=str(Path(d)/'x.db'); c=sqlite3.connect(p); c.executescript(SCHEMA.read_text()); c.close()
            code="import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute(\"INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,activated_ms,finished_ms) VALUES('p','m','t',1,2,3)\")"
            r=subprocess.run([sys.executable,'-c',code,p],capture_output=True,text=True); self.assertNotEqual(r.returncode,0); self.assertIn('cannot insert preactivated finished execution',r.stderr)
class Security(unittest.TestCase):
    def test_ignore_conflict_cannot_bypass(self):
        c=db()
        with self.assertRaises(sqlite3.IntegrityError): c.execute("INSERT OR IGNORE INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,activated_ms,finished_ms) VALUES('f','m','t',1,2,3)")
    def test_replace_cannot_bypass(self):
        c=db()
        with self.assertRaises(sqlite3.IntegrityError): c.execute("INSERT OR REPLACE INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,activated_ms,finished_ms) VALUES('g','m','t',1,2,3)")
    def test_valid_finalized_unactivated(self):
        c=db(); insert(c,'h',None,3,'completed'); self.assertEqual(c.execute('select outcome from mission_tool_execution_leases').fetchone()[0],'completed')
    def test_prior_guard_cancel(self):
        c=db()
        with self.assertRaises(sqlite3.IntegrityError): c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,activated_ms,cancel_requested_ms) VALUES('i','m','t',1,2,3)")
if __name__=='__main__': unittest.main()

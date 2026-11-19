"""V63: fail-closed preactivated INSERT without mission header."""
import sqlite3
import tempfile
import subprocess
import sys
import unittest
from pathlib import Path

SCHEMA = Path(__file__).resolve().parents[1] / "jarvis/missions/missions_schema.sql"

def db(path=":memory:"):
    conn = sqlite3.connect(path, timeout=3)
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    return conn

def lease(c, trace, mission="missing", activated=200):
    c.execute("INSERT INTO mission_tool_execution_leases (trace_id, mission_id, tool_name, started_ms, activated_ms) VALUES (?, ?, 'demo', 100, ?)", (trace, mission, activated))

def header(c, mission="present", state="RUNNING"):
    c.execute("INSERT INTO missions (id,prompt,state,created_ms,updated_ms) VALUES (?, 'demo', ?, 1, 1)", (mission, state))

class FunctionTests(unittest.TestCase):
    def test_missing_header_rejected(self):
        c=db()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "without mission header"): lease(c,'a')
    def test_existing_active_header_allowed(self):
        c=db();header(c);lease(c,'b','present');self.assertEqual(c.execute("SELECT activated_ms FROM mission_tool_execution_leases").fetchone()[0],200)
    def test_unactivated_reservation_still_allowed(self):
        c=db();lease(c,'c',activated=None);self.assertEqual(c.execute("SELECT count(*) FROM mission_tool_execution_leases").fetchone()[0],1)

class IntegrationTests(unittest.TestCase):
    def test_two_connections(self):
        with tempfile.TemporaryDirectory() as d:
            p=str(Path(d)/'db.sqlite');a=db(p);a.commit();b=sqlite3.connect(p);
            with self.assertRaises(sqlite3.IntegrityError):lease(b,'d')
            b.rollback();header(a);a.commit();lease(b,'e','present');b.commit();self.assertEqual(a.execute("SELECT count(*) FROM mission_tool_execution_leases").fetchone()[0],1);a.close();b.close()
    def test_separate_process(self):
        with tempfile.TemporaryDirectory() as d:
            p=str(Path(d)/'db.sqlite');c=db(p);c.commit();c.close()
            code="import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute(\"INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,activated_ms) VALUES('proc','missing','demo',1,2)\")"
            r=subprocess.run([sys.executable,'-c',code,p],capture_output=True,text=True);self.assertNotEqual(r.returncode,0);self.assertIn('without mission header',r.stderr)
    def test_transaction_rollback(self):
        c=db();c.execute('SAVEPOINT s')
        with self.assertRaises(sqlite3.IntegrityError):lease(c,'f')
        c.execute('ROLLBACK TO s');c.execute('RELEASE s');header(c);lease(c,'g','present');self.assertEqual(c.execute('SELECT count(*) FROM mission_tool_execution_leases').fetchone()[0],1)

class SecurityTests(unittest.TestCase):
    def test_insert_or_replace_cannot_bypass(self):
        c=db()
        with self.assertRaises(sqlite3.IntegrityError):c.execute("INSERT OR REPLACE INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,activated_ms) VALUES('h','missing','demo',1,2)")
    def test_insert_or_ignore_cannot_bypass(self):
        c=db()
        with self.assertRaises(sqlite3.IntegrityError):c.execute("INSERT OR IGNORE INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,activated_ms) VALUES('i','missing','demo',1,2)")
    def test_inactive_header_still_rejected(self):
        c=db();header(c,state='PAUSED')
        with self.assertRaises(sqlite3.IntegrityError):lease(c,'j','present')
    def test_no_ghost_record_after_rejection(self):
        c=db()
        with self.assertRaises(sqlite3.IntegrityError):lease(c,'k')
        self.assertEqual(c.execute('SELECT count(*) FROM mission_tool_execution_leases').fetchone()[0],0)

if __name__=='__main__':unittest.main()

import sqlite3, tempfile, subprocess, sys, unittest
from pathlib import Path
SCHEMA=Path(__file__).resolve().parents[2]/'jarvis/missions/missions_schema.sql'
def db(path=':memory:'):
 c=sqlite3.connect(path);c.executescript(SCHEMA.read_text());return c
def header(c,state='PAUSED'):
 c.execute("INSERT INTO missions(id,state,prompt,created_ms,updated_ms) VALUES('m',?,'test',1,1)",(state,))
def insert(c,trace='t',mission='m',active=2):
 c.execute('INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,activated_ms) VALUES(?,?,?,1,?)',(trace,mission,'tool',active))
class Function(unittest.TestCase):
 def test_paused_blocked(self):
  c=db();header(c);self.assertRaises(sqlite3.IntegrityError,insert,c)
 def test_cancelled_blocked(self):
  c=db();header(c,'CANCELLED');self.assertRaises(sqlite3.IntegrityError,insert,c)
 def test_running_allowed(self):
  c=db();header(c,'RUNNING');insert(c);self.assertEqual(c.execute('SELECT activated_ms FROM mission_tool_execution_leases').fetchone()[0],2)
class Integration(unittest.TestCase):
 def test_second_connection(self):
  with tempfile.TemporaryDirectory() as d:
   p=str(Path(d)/'db');a=db(p);header(a);a.commit();b=sqlite3.connect(p);self.assertRaises(sqlite3.IntegrityError,insert,b);self.assertEqual(a.execute('SELECT count(*) FROM mission_tool_execution_leases').fetchone()[0],0);a.close();b.close()
 def test_separate_process(self):
  with tempfile.TemporaryDirectory() as d:
   p=str(Path(d)/'db');a=db(p);header(a);a.commit();a.close()
   code="import sqlite3,sys;c=sqlite3.connect(sys.argv[1]);c.execute(\"INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,activated_ms) VALUES('t','m','tool',1,2)\")"
   r=subprocess.run([sys.executable,'-c',code,p],capture_output=True,text=True);self.assertNotEqual(r.returncode,0);self.assertIn('inactive mission',r.stderr)
 def test_savepoint_rollback(self):
  c=db();header(c);c.execute('SAVEPOINT s');self.assertRaises(sqlite3.IntegrityError,insert,c);c.execute('ROLLBACK TO s');c.execute('RELEASE s');self.assertEqual(c.execute('SELECT count(*) FROM mission_tool_execution_leases').fetchone()[0],0)
class Security(unittest.TestCase):
 def test_insert_or_replace_blocked(self):
  c=db();header(c);self.assertRaises(sqlite3.IntegrityError,lambda:c.execute("INSERT OR REPLACE INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,activated_ms) VALUES('t','m','tool',1,2)"))
 def test_insert_or_ignore_blocked(self):
  c=db();header(c);self.assertRaises(sqlite3.IntegrityError,lambda:c.execute("INSERT OR IGNORE INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,activated_ms) VALUES('t','m','tool',1,2)"))
 def test_unactivated_compatible(self):
  c=db();header(c);insert(c,active=None);self.assertIsNone(c.execute('SELECT activated_ms FROM mission_tool_execution_leases').fetchone()[0])
 def test_missing_header_legacy_compatible(self):
  c=db();insert(c,mission='missing');self.assertEqual(c.execute('SELECT count(*) FROM mission_tool_execution_leases').fetchone()[0],1)
if __name__=='__main__':unittest.main()

import sqlite3, tempfile, subprocess, sys, unittest
from pathlib import Path
SCHEMA=Path(__file__).resolve().parents[2]/'jarvis/missions/missions_schema.sql'
def connect(path=':memory:'):
 c=sqlite3.connect(path);c.executescript(SCHEMA.read_text());return c
def setup(c, state='PAUSED', trace='t'):
 # The header is created before a legacy unactivated lease. No event latch is present.
 c.execute("INSERT INTO missions(id,state,prompt,created_ms,updated_ms) VALUES(?,?,'test',1,1)",('m',state))
 c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms) VALUES(?,?,?,?)",(trace,'m','tool',1))
def activate(c,trace='t'):
 c.execute('UPDATE mission_tool_execution_leases SET activated_ms=2 WHERE trace_id=?',(trace,))
class Function(unittest.TestCase):
 def test_paused_rejected(self):
  c=connect();setup(c);self.assertRaises(sqlite3.IntegrityError,activate,c)
 def test_cancelled_rejected(self):
  c=connect();setup(c,'CANCELLED');self.assertRaises(sqlite3.IntegrityError,activate,c)
 def test_active_allowed(self):
  c=connect();setup(c,'RUNNING');activate(c);self.assertEqual(c.execute('SELECT activated_ms FROM mission_tool_execution_leases').fetchone()[0],2)
class Integration(unittest.TestCase):
 def test_two_connections(self):
  with tempfile.TemporaryDirectory() as d:
   p=str(Path(d)/'x.db');a=connect(p);setup(a);a.commit();b=sqlite3.connect(p)
   self.assertRaises(sqlite3.IntegrityError,activate,b);self.assertIsNone(a.execute('SELECT activated_ms FROM mission_tool_execution_leases').fetchone()[0]);a.close();b.close()
 def test_other_process(self):
  with tempfile.TemporaryDirectory() as d:
   p=str(Path(d)/'x.db');a=connect(p);setup(a);a.commit();a.close()
   code="import sqlite3,sys;c=sqlite3.connect(sys.argv[1]);c.execute('UPDATE mission_tool_execution_leases SET activated_ms=2 WHERE trace_id=\"t\"')"
   r=subprocess.run([sys.executable,'-c',code,p],capture_output=True,text=True);self.assertNotEqual(r.returncode,0);self.assertIn('inactive mission',r.stderr)
 def test_savepoint(self):
  c=connect();setup(c);c.execute('SAVEPOINT s');self.assertRaises(sqlite3.IntegrityError,activate,c);c.execute('ROLLBACK TO s');c.execute('RELEASE s');self.assertIsNone(c.execute('SELECT activated_ms FROM mission_tool_execution_leases').fetchone()[0])
class Security(unittest.TestCase):
 def test_replace_update_rejected(self):
  c=connect();setup(c);self.assertRaises(sqlite3.IntegrityError,lambda:c.execute("UPDATE OR REPLACE mission_tool_execution_leases SET activated_ms=2 WHERE trace_id='t'"))
 def test_ignore_update_rejected(self):
  c=connect();setup(c);self.assertRaises(sqlite3.IntegrityError,lambda:c.execute("UPDATE OR IGNORE mission_tool_execution_leases SET activated_ms=2 WHERE trace_id='t'"))
 def test_missing_header_legacy_compatible(self):
  c=connect();c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms) VALUES('t','absent','tool',1)");activate(c);self.assertEqual(c.execute('SELECT activated_ms FROM mission_tool_execution_leases').fetchone()[0],2)
 def test_idempotent_existing_activation(self):
  c=connect();setup(c,'RUNNING');activate(c);c.execute("UPDATE missions SET state='PAUSED' WHERE id='m'");c.execute("UPDATE mission_tool_execution_leases SET activated_ms=activated_ms WHERE trace_id='t'");self.assertEqual(c.execute('SELECT activated_ms FROM mission_tool_execution_leases').fetchone()[0],2)
if __name__=='__main__':unittest.main()

import sqlite3, tempfile, subprocess, sys, unittest
from pathlib import Path
SCHEMA=Path(__file__).resolve().parents[2]/'jarvis/missions/missions_schema.sql'
def db(path=':memory:'):
 c=sqlite3.connect(path);c.executescript(SCHEMA.read_text());return c
def reserve(c,mission='missing',trace='t'):
 c.execute('INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms) VALUES(?,?,?,1)',(trace,mission,'tool'))
def activate(c,trace='t'):
 c.execute('UPDATE mission_tool_execution_leases SET activated_ms=2 WHERE trace_id=?',(trace,))
def header(c,state='RUNNING'):
 c.execute("INSERT INTO missions(id,state,prompt,created_ms,updated_ms) VALUES('m',?,'test',1,1)",(state,))
class Function(unittest.TestCase):
 def test_missing_rejected(self):
  c=db();reserve(c);self.assertRaisesRegex(sqlite3.IntegrityError,'without mission header',activate,c)
 def test_active_header_allowed(self):
  c=db();header(c);reserve(c,'m');activate(c);self.assertEqual(c.execute('SELECT activated_ms FROM mission_tool_execution_leases').fetchone()[0],2)
 def test_reservation_without_header_allowed(self):
  c=db();reserve(c);self.assertIsNone(c.execute('SELECT activated_ms FROM mission_tool_execution_leases').fetchone()[0])
class Integration(unittest.TestCase):
 def test_two_connections(self):
  with tempfile.TemporaryDirectory() as d:
   p=str(Path(d)/'x.db');a=db(p);reserve(a);a.commit();b=sqlite3.connect(p);self.assertRaises(sqlite3.IntegrityError,activate,b);self.assertIsNone(a.execute('SELECT activated_ms FROM mission_tool_execution_leases').fetchone()[0]);a.close();b.close()
 def test_other_process(self):
  with tempfile.TemporaryDirectory() as d:
   p=str(Path(d)/'x.db');a=db(p);reserve(a);a.commit();a.close()
   code = 'import sqlite3,sys;c=sqlite3.connect(sys.argv[1]);c.execute("UPDATE mission_tool_execution_leases SET activated_ms=2 WHERE trace_id=\'t\'")'
   r=subprocess.run([sys.executable,'-c',code,p],capture_output=True,text=True);self.assertNotEqual(r.returncode,0);self.assertIn('without mission header',r.stderr)
 def test_savepoint(self):
  c=db();reserve(c);c.execute('SAVEPOINT s');self.assertRaises(sqlite3.IntegrityError,activate,c);c.execute('ROLLBACK TO s');c.execute('RELEASE s');self.assertIsNone(c.execute('SELECT activated_ms FROM mission_tool_execution_leases').fetchone()[0])
class Security(unittest.TestCase):
 def test_update_or_replace(self):
  c=db();reserve(c);self.assertRaises(sqlite3.IntegrityError,lambda:c.execute("UPDATE OR REPLACE mission_tool_execution_leases SET activated_ms=2 WHERE trace_id='t'"))
 def test_update_or_ignore(self):
  c=db();reserve(c);self.assertRaises(sqlite3.IntegrityError,lambda:c.execute("UPDATE OR IGNORE mission_tool_execution_leases SET activated_ms=2 WHERE trace_id='t'"))
 def test_missing_header_idempotent_existing_activation(self):
  c=db();header(c);reserve(c,'m');activate(c);c.execute("DELETE FROM missions WHERE id='m'");c.execute("UPDATE mission_tool_execution_leases SET activated_ms=activated_ms WHERE trace_id='t'");self.assertEqual(c.execute('SELECT activated_ms FROM mission_tool_execution_leases').fetchone()[0],2)
 def test_header_recreated_active(self):
  c=db();reserve(c);c.execute("INSERT INTO missions(id,state,prompt,created_ms,updated_ms) VALUES('missing','RUNNING','test',1,1)");activate(c);self.assertEqual(c.execute('SELECT activated_ms FROM mission_tool_execution_leases').fetchone()[0],2)
if __name__=='__main__':unittest.main()

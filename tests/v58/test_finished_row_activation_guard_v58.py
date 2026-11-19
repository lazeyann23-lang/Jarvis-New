"""V58: a durable finish timestamp fences first activation even with running outcome."""
import pathlib,sqlite3,subprocess,sys,unittest,tempfile
SCHEMA=pathlib.Path(__file__).resolve().parents[2]/'jarvis/missions/missions_schema.sql'
class FinishedRowActivationTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.path=pathlib.Path(self.temp.name)/'db.sqlite'
  self.c=sqlite3.connect(self.path);self.c.executescript(SCHEMA.read_text());self.c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,finished_ms) VALUES('bad','m','tool',1,2)");self.c.commit()
 def tearDown(self):self.c.close();self.temp.cleanup()
 def reject(self,c,sql="UPDATE mission_tool_execution_leases SET activated_ms=3 WHERE trace_id='bad'"):
  with self.assertRaisesRegex(sqlite3.IntegrityError,'cannot activate finished execution timestamp'):c.execute(sql)
 def state(self,trace='bad'):return self.c.execute('SELECT activated_ms FROM mission_tool_execution_leases WHERE trace_id=?',(trace,)).fetchone()[0]
 # Function tests
 def test_function_running_finished_rejected(self):self.reject(self.c);self.assertIsNone(self.state())
 def test_function_simultaneous_finish_and_activate_rejected(self):
  self.c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms) VALUES('fresh','m','tool',1)");self.reject(self.c,"UPDATE mission_tool_execution_leases SET activated_ms=3,finished_ms=4 WHERE trace_id='fresh'")
 def test_function_ordinary_activation_allowed(self):
  self.c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms) VALUES('fresh','m','tool',1)");self.c.execute("UPDATE mission_tool_execution_leases SET activated_ms=3 WHERE trace_id='fresh'");self.assertEqual(self.state('fresh'),3)
 # Integration tests
 def test_integration_second_connection(self):
  d=sqlite3.connect(self.path);self.reject(d);d.close();self.assertIsNone(self.state())
 def test_integration_separate_process(self):
  code="import sqlite3,sys;c=sqlite3.connect(sys.argv[1]);c.execute(\"UPDATE mission_tool_execution_leases SET activated_ms=3 WHERE trace_id='bad'\")"
  r=subprocess.run([sys.executable,'-c',code,str(self.path)],capture_output=True,text=True,timeout=10);self.assertNotEqual(r.returncode,0);self.assertIn('cannot activate finished execution timestamp',r.stderr)
 def test_integration_savepoint(self):
  self.c.execute('SAVEPOINT x');self.reject(self.c);self.c.execute('ROLLBACK TO x');self.c.execute('RELEASE x');self.assertIsNone(self.state())
 # Security/regression tests
 def test_security_multirow_statement_atomic(self):
  self.c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms) VALUES('fresh','m','tool',1)");self.reject(self.c,"UPDATE mission_tool_execution_leases SET activated_ms=3 WHERE mission_id='m'");self.assertIsNone(self.state('fresh'))
 def test_security_finish_latch_unchanged(self):
  with self.assertRaises(sqlite3.IntegrityError):self.c.execute("UPDATE mission_tool_execution_leases SET finished_ms=NULL WHERE trace_id='bad'")
 def test_security_existing_activation_idempotent(self):
  self.c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,activated_ms) VALUES('old','m','tool',1,3)");self.c.execute("UPDATE mission_tool_execution_leases SET activated_ms=3 WHERE trace_id='old'");self.assertEqual(self.state('old'),3)
 def test_security_cancel_latch_still_blocks(self):
  self.c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,cancel_requested_ms) VALUES('cancel','m','tool',1,2)");
  with self.assertRaises(sqlite3.IntegrityError):self.c.execute("UPDATE mission_tool_execution_leases SET activated_ms=3 WHERE trace_id='cancel'")
if __name__=='__main__':unittest.main()

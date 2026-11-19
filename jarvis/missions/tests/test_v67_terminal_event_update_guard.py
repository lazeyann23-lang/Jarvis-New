import sqlite3, tempfile, pathlib, subprocess, sys, unittest, os
SCHEMA = pathlib.Path(__file__).resolve().parent.parent.joinpath('missions_schema.sql').read_text()
TERMINAL = [('MissionCancelled','{}'),('MissionStateChanged','{"to_state":"CANCELLED"}'),('MissionStateChanged','not json')]
class GuardTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.path=str(pathlib.Path(self.tmp.name)/'db.sqlite'); self.db=sqlite3.connect(self.path); self.db.executescript(SCHEMA)
  self.db.execute("INSERT INTO mission_events(event_id,mission_id,event_type,source_actor,ts_ms,payload_json) VALUES ('ev','m',?,?,1,?)",('MissionCancelled','system','{}'));self.db.commit()
 def tearDown(self):self.db.close();self.tmp.cleanup()
 def test_function_change_type(self):
  with self.assertRaises(sqlite3.IntegrityError):self.db.execute("UPDATE mission_events SET event_type='MissionDispatched' WHERE event_id='ev'")
 def test_function_change_payload(self):
  with self.assertRaises(sqlite3.IntegrityError):self.db.execute("UPDATE mission_events SET payload_json='{}' WHERE event_id='ev'")
 def test_function_nonterminal_edit(self):
  self.db.execute("INSERT INTO mission_events(event_id,mission_id,event_type,source_actor,ts_ms,payload_json) VALUES ('other','m','MissionDispatched','system',1,'{}')");self.db.execute("UPDATE mission_events SET ts_ms=2 WHERE event_id='other'");self.assertEqual(self.db.execute("SELECT ts_ms FROM mission_events WHERE event_id='other'").fetchone()[0],2)
 def test_integration_second_connection(self):
  other=sqlite3.connect(self.path)
  try:
   with self.assertRaises(sqlite3.IntegrityError):other.execute("UPDATE mission_events SET mission_id='other' WHERE event_id='ev'")
  finally:other.close()
 def test_integration_process(self):
  script = "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute(\"UPDATE mission_events SET event_type='MissionDispatched' WHERE event_id='ev'\")"
  p=subprocess.run([sys.executable,'-c',script,self.path],capture_output=True,text=True)
  self.assertNotEqual(p.returncode,0);self.assertIn('terminal mission event cannot be updated',p.stderr)
 def test_integration_savepoint(self):
  self.db.execute('SAVEPOINT x')
  with self.assertRaises(sqlite3.IntegrityError):self.db.execute("UPDATE mission_events SET ts_ms=3 WHERE event_id='ev'")
  self.db.execute('ROLLBACK TO x');self.db.execute('RELEASE x');self.assertEqual(self.db.execute("SELECT ts_ms FROM mission_events WHERE event_id='ev'").fetchone()[0],1)
 def test_security_rename_id(self):
  with self.assertRaises(sqlite3.IntegrityError):self.db.execute("UPDATE mission_events SET event_id='new' WHERE event_id='ev'")
 def test_security_terminal_state(self):
  self.db.execute("INSERT INTO mission_events(event_id,mission_id,event_type,source_actor,ts_ms,payload_json) VALUES ('s','m','MissionStateChanged','system',1,'{\"to_state\":\"FAILED\"}')")
  with self.assertRaises(sqlite3.IntegrityError):self.db.execute("UPDATE mission_events SET payload_json='{\"to_state\":\"RUNNING\"}' WHERE event_id='s'")
 def test_security_malformed_state(self):
  self.db.execute("INSERT INTO mission_events(event_id,mission_id,event_type,source_actor,ts_ms,payload_json) VALUES ('s','m','MissionStateChanged','system',1,'bad')")
  with self.assertRaises(sqlite3.IntegrityError):self.db.execute("UPDATE mission_events SET event_type='MissionDispatched' WHERE event_id='s'")
 def test_security_ordinary_event(self):
  self.db.execute("INSERT INTO mission_events(event_id,mission_id,event_type,source_actor,ts_ms,payload_json) VALUES ('s','m','MissionStateChanged','system',1,'{\"to_state\":\"RUNNING\"}')")
  self.db.execute("UPDATE mission_events SET ts_ms=3 WHERE event_id='s'");self.assertEqual(self.db.execute("SELECT ts_ms FROM mission_events WHERE event_id='s'").fetchone()[0],3)
if __name__=='__main__':unittest.main(verbosity=2)

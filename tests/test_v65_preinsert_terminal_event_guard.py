import sqlite3, tempfile, subprocess, sys, unittest
from pathlib import Path
SCHEMA=Path(__file__).resolve().parents[1]/'jarvis/missions/missions_schema.sql'
class V65Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.db=Path(self.tmp.name)/'db.sqlite'; self.c=sqlite3.connect(self.db); self.c.executescript(SCHEMA.read_text()); self.c.execute("INSERT INTO missions(id,prompt,state,created_ms,updated_ms) VALUES('m','p','RUNNING',1,1)");self.c.commit()
 def tearDown(self): self.c.close();self.tmp.cleanup()
 def event(self,kind='MissionFailed',payload='{}'):
  self.c.execute('INSERT INTO mission_events(event_id,mission_id,event_type,source_actor,ts_ms,payload_json) VALUES(?,?,?,?,?,?)',(kind,'m',kind,'system',2,payload));self.c.commit()
 def insert(self,trace='t',active=True,conn=None):
  (conn or self.c).execute('INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,activated_ms) VALUES(?,?,?,?,?)',(trace,'m','x',1,3 if active else None))
 # Functional
 def test_01_normal_preactivated_insert_allowed(self): self.insert()
 def test_02_terminal_event_blocks_preactivated_insert(self):
  self.event();
  with self.assertRaises(sqlite3.IntegrityError): self.insert()
 def test_03_terminal_event_allows_unactivated_reservation(self): self.event();self.insert(active=False)
 # Integration
 def test_04_second_connection_rejects(self):
  self.event('MissionCancelled');other=sqlite3.connect(self.db)
  try:
   with self.assertRaises(sqlite3.IntegrityError): self.insert(conn=other)
  finally: other.close()
 def test_05_subprocess_rejects(self):
  self.event('MissionTimedOut');code="""import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,activated_ms) VALUES('t','m','x',1,3)")"""
  p=subprocess.run([sys.executable,'-c',code,str(self.db)],capture_output=True,text=True);self.assertNotEqual(p.returncode,0);self.assertIn('terminal mission event',p.stderr)
 def test_06_rollback_restores_permission(self):
  self.c.execute('BEGIN');self.c.execute("INSERT INTO mission_events(event_id,mission_id,event_type,source_actor,ts_ms,payload_json) VALUES('e','m','MissionFailed','system',2,'{}')");self.c.rollback();self.insert()
 # Security and regression
 def test_07_malformed_state_event_blocks(self):
  self.event('MissionStateChanged','not json')
  with self.assertRaises(sqlite3.IntegrityError): self.insert()
 def test_08_active_state_event_allows(self): self.event('MissionStateChanged','{"to_state":"RUNNING"}');self.insert()
 def test_09_reactivated_header_does_not_override_terminal_event(self):
  self.event();self.c.execute("UPDATE missions SET state='RUNNING' WHERE id='m'");self.c.commit()
  with self.assertRaises(sqlite3.IntegrityError): self.insert()
 def test_10_savepoint_rollback_does_not_bypass(self):
  self.event();self.c.execute('SAVEPOINT s')
  with self.assertRaises(sqlite3.IntegrityError): self.insert()
  self.c.execute('ROLLBACK TO s');self.c.execute('RELEASE s')
  with self.assertRaises(sqlite3.IntegrityError): self.insert()
if __name__=='__main__':unittest.main(verbosity=2)

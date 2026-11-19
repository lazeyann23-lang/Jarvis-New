import sqlite3, tempfile, subprocess, sys, unittest
from pathlib import Path

SCHEMA = Path(__file__).resolve().parents[1] / 'jarvis/missions/missions_schema.sql'

class V64TerminalEventActivationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'test.db'
        self.conn = sqlite3.connect(self.db)
        self.conn.executescript(SCHEMA.read_text(encoding='utf-8'))
        self.conn.execute("INSERT INTO missions(id,prompt,state,created_ms,updated_ms) VALUES ('m','p','RUNNING',1,1)")
        self.conn.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms) VALUES ('t','m','x',1)")
        self.conn.commit()
    def tearDown(self):
        self.conn.close(); self.tmp.cleanup()
    def event(self, kind, payload='{}'):
        self.conn.execute("INSERT INTO mission_events(event_id,mission_id,event_type,source_actor,ts_ms,payload_json) VALUES (?,?,?,?,?,?)",(kind,'m',kind,'system',2,payload))
        self.conn.commit()
    def activate(self):
        self.conn.execute("UPDATE mission_tool_execution_leases SET activated_ms=3 WHERE trace_id='t'")
        self.conn.commit()
    # Functional
    def test_01_active_header_allows_activation(self): self.activate()
    def test_02_terminal_event_rejects_activation(self):
        self.event('MissionFailed')
        with self.assertRaises(sqlite3.IntegrityError): self.activate()
    def test_03_invalid_state_change_payload_blocks_activation(self):
        self.event('MissionStateChanged','not json')
        with self.assertRaises(sqlite3.IntegrityError): self.activate()
    # Integration: independent SQLite connections/processes
    def test_04_second_connection_observes_terminal_event(self):
        self.event('MissionCancelled')
        other=sqlite3.connect(self.db)
        try:
            with self.assertRaises(sqlite3.IntegrityError): other.execute("UPDATE mission_tool_execution_leases SET activated_ms=3 WHERE trace_id='t'")
        finally: other.close()
    def test_05_subprocess_observes_terminal_event(self):
        self.event('MissionTimedOut')
        code="import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute(\"UPDATE mission_tool_execution_leases SET activated_ms=3 WHERE trace_id='t'\")"
        p=subprocess.run([sys.executable,'-c',code,str(self.db)],capture_output=True,text=True)
        self.assertNotEqual(p.returncode,0); self.assertIn('terminal mission event',p.stderr)
    def test_06_rollback_does_not_persist_event(self):
        self.conn.execute('BEGIN')
        self.conn.execute("INSERT INTO mission_events(event_id,mission_id,event_type,source_actor,ts_ms,payload_json) VALUES ('e','m','MissionFailed','system',2,'{}')")
        self.conn.rollback(); self.activate()
    # Security/regression
    def test_07_terminal_event_blocks_after_header_reactivation(self):
        self.event('MissionFailed')
        self.conn.execute("UPDATE missions SET state='RUNNING' WHERE id='m'")
        with self.assertRaises(sqlite3.IntegrityError): self.activate()
    def test_08_valid_running_state_change_does_not_block(self):
        self.event('MissionStateChanged','{"to_state":"RUNNING"}')
        self.activate()
    def test_09_terminal_state_change_blocks(self):
        self.event('MissionStateChanged','{"to_state":"COMPLETED"}')
        with self.assertRaises(sqlite3.IntegrityError): self.activate()
    def test_10_idempotent_unactivated_update_allowed(self):
        self.event('MissionFailed')
        self.conn.execute("UPDATE mission_tool_execution_leases SET activated_ms=NULL WHERE trace_id='t'")

if __name__=='__main__': unittest.main()

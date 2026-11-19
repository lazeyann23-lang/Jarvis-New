import sqlite3, tempfile, pathlib, subprocess, sys, unittest
SCHEMA = pathlib.Path(__file__).resolve().parents[1] / "missions_schema.sql"
class TerminalDeleteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=pathlib.Path(self.tmp.name)/"missions.db"
        self.db=sqlite3.connect(self.path); self.db.executescript(SCHEMA.read_text())
    def tearDown(self): self.db.close(); self.tmp.cleanup()
    def event(self, kind, payload='{}', ident='e1'):
        self.db.execute("INSERT INTO mission_events(event_id,mission_id,event_type,source_actor,ts_ms,payload_json) VALUES (?,?,?,?,?,?)",(ident,'m1',kind,'system',1,payload)); self.db.commit()
    def count(self): return self.db.execute('SELECT count(*) FROM mission_events').fetchone()[0]
    def test_function_cancel_delete_blocked(self):
        self.event('MissionCancelled');
        with self.assertRaises(sqlite3.IntegrityError): self.db.execute('DELETE FROM mission_events WHERE event_id=?',('e1',))
        self.assertEqual(self.count(),1)
    def test_function_terminal_state_delete_blocked(self):
        self.event('MissionStateChanged','{"to_state":"PAUSED"}')
        with self.assertRaises(sqlite3.IntegrityError): self.db.execute('DELETE FROM mission_events')
        self.assertEqual(self.count(),1)
    def test_function_active_event_delete_allowed(self):
        self.event('MissionStateChanged','{"to_state":"RUNNING"}'); self.db.execute('DELETE FROM mission_events'); self.db.commit(); self.assertEqual(self.count(),0)
    def test_integration_second_connection(self):
        self.event('MissionFailed'); other=sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError): other.execute('DELETE FROM mission_events')
            self.assertEqual(self.count(),1)
        finally: other.close()
    def test_integration_subprocess(self):
        self.event('MissionTimedOut')
        script='import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute("DELETE FROM mission_events"); c.commit()'
        p=subprocess.run([sys.executable,'-c',script,str(self.path)],capture_output=True,text=True)
        self.assertNotEqual(p.returncode,0); self.assertIn('terminal mission event cannot be deleted',p.stderr); self.assertEqual(self.count(),1)
    def test_integration_transaction_rollback(self):
        self.event('MissionApproved'); self.db.execute('SAVEPOINT test')
        with self.assertRaises(sqlite3.IntegrityError): self.db.execute('DELETE FROM mission_events')
        self.db.execute('ROLLBACK TO test'); self.db.execute('RELEASE test'); self.assertEqual(self.count(),1)
    def test_security_invalid_state_payload(self):
        self.event('MissionStateChanged','not-json')
        with self.assertRaises(sqlite3.IntegrityError): self.db.execute('DELETE FROM mission_events')
    def test_security_missing_state(self):
        self.event('MissionStateChanged','{}')
        with self.assertRaises(sqlite3.IntegrityError): self.db.execute('DELETE FROM mission_events')
    def test_security_other_terminal_types(self):
        for i,k in enumerate(['MissionApproved','MissionFailed','MissionCancelled','MissionTimedOut']): self.event(k,ident=f'e{i}')
        with self.assertRaises(sqlite3.IntegrityError): self.db.execute('DELETE FROM mission_events')
        self.assertEqual(self.count(),4)
    def test_security_nonterminal_preserved(self):
        self.event('MissionDispatched'); self.db.execute('DELETE FROM mission_events'); self.db.commit(); self.assertEqual(self.count(),0)
if __name__=='__main__': unittest.main(verbosity=2)

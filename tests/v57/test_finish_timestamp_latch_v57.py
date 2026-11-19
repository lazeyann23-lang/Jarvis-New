"""V57 regression: run with python -m unittest tests/v57/test_finish_timestamp_latch_v57.py"""
import sqlite3
import unittest
from pathlib import Path

SCHEMA = Path(__file__).resolve().parents[2] / 'jarvis' / 'missions' / 'missions_schema.sql'

class FinishTimestampLatchTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.executescript(SCHEMA.read_text())
        self.db.execute("INSERT INTO mission_tool_execution_leases(trace_id,mission_id,tool_name,started_ms,finished_ms,outcome) VALUES('t','m','tool',1,2,'completed')")

    def tearDown(self): self.db.close()

    def test_rewrite_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE mission_tool_execution_leases SET finished_ms=3 WHERE trace_id='t'")

    def test_clear_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE mission_tool_execution_leases SET finished_ms=NULL WHERE trace_id='t'")

    def test_idempotent_allowed(self):
        self.db.execute("UPDATE mission_tool_execution_leases SET finished_ms=2 WHERE trace_id='t'")
        self.assertEqual(self.db.execute("SELECT finished_ms FROM mission_tool_execution_leases WHERE trace_id='t'").fetchone()[0], 2)

if __name__ == '__main__': unittest.main()

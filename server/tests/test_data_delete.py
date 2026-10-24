"""Deleting measured data (Nastavení → Obecné → Záloha a data): dry run, range, all, audit kept."""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from retencka.core import Core  # noqa: E402
from retencka.db import Database  # noqa: E402
from retencka.settings import Settings  # noqa: E402

A, B = 0x0A01, 0x0B02
T0 = 1_760_000_400 - 1_760_000_400 % 3600          # whole hour


class DataDelete(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Database(os.path.join(self.tmp.name, "t.db"))
        self.core = Core(self.db, Settings(self.db), self.tmp.name)
        self.core.nodes.add(A, "Zahrada")
        self.core.nodes.add(B, "Garáž")
        with self.db.transaction() as c:
            for nid in (A, B):
                for i in range(48):                      # 24 h every 30 min = 48 rows, 24 hours
                    c.execute("INSERT INTO measurements(ts, node_id, seq, flags, level_mm, volume_l) "
                              "VALUES(?,?,?,0,?,?)", (T0 + i * 1800, nid, i, 500 + i, 5000 + i))
                c.execute("INSERT INTO refills(start_ts, end_ts, node_id, mode, target_pct) VALUES(?,?,?,'manual',50)", (T0 + 3600, T0 + 4000, nid))
                c.execute("INSERT INTO refills(start_ts, end_ts, node_id, mode, target_pct) VALUES(?,?,?,'manual',50)", (T0 + 20 * 3600, T0 + 20 * 3600 + 300, nid))
            c.execute("INSERT INTO refills(start_ts, end_ts, node_id, mode, target_pct) VALUES(?,?,?,'manual',50)", (T0 + 5 * 3600, None, A))  # running
        self.core.aggregate_hours(since=0)
        self.core.audit("LOGIN_SUCCESS", "před mazáním", "admin")

    def tearDown(self):
        self.db.close_thread()
        self.tmp.cleanup()

    def count(self, table, nid):
        return self.db.scalar(f"SELECT COUNT(*) FROM {table} WHERE node_id=?", (nid,))

    def test_dry_run_counts_without_deleting(self):
        c = self.core.count_data(A, T0, T0 + 6 * 3600)            # 12 measurements, 6 hours, 1 finished refill
        self.assertEqual(c, {"measurements": 12, "hourly": 6, "refills": 1})
        self.assertEqual(self.count("measurements", A), 48)
        self.assertEqual(self.core.count_data(None, None, None), {"measurements": 96, "hourly": 48, "refills": 4})

    def test_range_delete_one_tank(self):
        got = self.core.delete_data(A, T0 + 2 * 3600 + 900, T0 + 6 * 3600)    # starts inside an hour
        self.core.after_delete(A)
        self.assertEqual(got["measurements"], 7)
        self.assertEqual(self.count("measurements", A), 41)
        self.assertEqual(self.count("measurements", B), 48)             # other tank untouched
        # the partly kept edge hour is rebuilt from the remaining raw data
        h = self.db.one("SELECT n FROM hourly WHERE node_id=? AND hour=?", (A, T0 + 2 * 3600))
        self.assertEqual(h["n"], 1)
        self.assertIsNone(self.db.one("SELECT 1 FROM hourly WHERE node_id=? AND hour=?", (A, T0 + 3 * 3600)))
        self.assertEqual(self.count("hourly", B), 24)
        # running refill (no end) stays
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM refills WHERE node_id=? AND end_ts IS NULL", (A,)), 1)

    def test_delete_all_keeps_audit(self):
        audit_before = self.db.scalar("SELECT COUNT(*) FROM audit")
        got = self.core.delete_data(None, None, None)
        self.core.after_delete(None)
        self.assertEqual(got, {"measurements": 96, "hourly": 48, "refills": 4})
        for t in ("measurements", "hourly"):
            self.assertEqual(self.db.scalar(f"SELECT COUNT(*) FROM {t}"), 0)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM refills"), 1)    # the running one
        self.assertGreaterEqual(self.db.scalar("SELECT COUNT(*) FROM audit"), audit_before)
        self.assertIsNone(self.core.last.get(A))                                # live state reloaded
        self.assertEqual(len(self.core.nodes.ids()), 2)                         # tanks stay


if __name__ == "__main__":
    unittest.main()

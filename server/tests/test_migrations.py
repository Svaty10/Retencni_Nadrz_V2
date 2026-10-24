"""DB migrations: a database created by the very first version (schema v1, with data)
reaches the latest schema with its data intact – and looks exactly like a fresh one."""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from retencka import db as db_mod  # noqa: E402
from retencka.core import Core  # noqa: E402
from retencka.db import MIGRATIONS, Database  # noqa: E402
from retencka.settings import Settings  # noqa: E402


def schema(path: str) -> dict:
    """{table: [(column, type, notnull, default, pk)], "index:<name>": sql}"""
    con = sqlite3.connect(path)
    try:
        out = {}
        for name, typ, sql in con.execute("SELECT name, type, sql FROM sqlite_master "
                                          "WHERE name NOT LIKE 'sqlite_%' ORDER BY name"):
            if typ == "table":
                out[name] = sorted(tuple(r[1:]) for r in con.execute(f"PRAGMA table_info({name})"))
            elif typ == "index":
                out["index:" + name] = sorted(r[2] for r in con.execute(f"PRAGMA index_info({name})"))
        return out
    finally:
        con.close()


def make_v1(path: str) -> None:
    con = sqlite3.connect(path)
    for stmt in db_mod._split_sql(MIGRATIONS[0]):
        con.execute(stmt)
    con.execute("PRAGMA user_version=1")
    con.executemany("INSERT INTO measurements(ts, node_id, seq, flags, level_mm, volume_l) VALUES(?,?,?,0,?,?)",
                    [(1_700_000_000 + i * 600, 1, i, 400 + i, 4000 + i) for i in range(10)])
    con.execute("INSERT INTO settings(key, value) VALUES('location_lat', '49.5')")
    con.execute("INSERT INTO users(username, pw_hash, role, created, pw_changed) VALUES('Pepa','h','admin',1,1)")
    con.execute("INSERT INTO audit(ts, username, action, detail) VALUES(1, 'Pepa', 'LOGIN_SUCCESS', 'v1')")
    con.execute("INSERT INTO refills(start_ts, end_ts, mode, target_pct) VALUES(1, 2, 'manual', 50)")
    con.execute("INSERT INTO hourly(hour, n, level_mm) VALUES(3600, 6, 400)")      # v1 shape: dropped in v2
    con.commit()
    con.close()


class MigrationChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.old = os.path.join(self.tmp.name, "old.db")
        self.fresh = os.path.join(self.tmp.name, "fresh.db")
        self.opened = []

    def tearDown(self):
        for d in self.opened:
            d.close_thread()
        self.tmp.cleanup()

    def open(self, path: str) -> Database:
        d = Database(path)
        self.opened.append(d)
        return d

    def test_v1_to_latest_keeps_data_and_matches_fresh_schema(self):
        make_v1(self.old)
        d = self.open(self.old)
        self.assertEqual(d.scalar("PRAGMA user_version"), len(MIGRATIONS))
        self.open(self.fresh)
        self.assertEqual(schema(self.old), schema(self.fresh))
        self.assertEqual(d.scalar("SELECT COUNT(*) FROM measurements"), 10)
        self.assertEqual(d.scalar("SELECT MAX(level_mm) FROM measurements"), 409)
        self.assertEqual(d.scalar("SELECT value FROM settings WHERE key='location_lat'"), "49.5")
        u = d.one("SELECT role, theme FROM users WHERE username='pepa'")         # NOCASE kept
        self.assertEqual((u["role"], u["theme"]), ("admin", "auto"))
        self.assertEqual(d.scalar("SELECT detail FROM audit"), "v1")
        self.assertEqual(d.scalar("SELECT COUNT(*) FROM refills"), 1)
        self.assertTrue(d.integrity_ok())
        # the application starts on the migrated database and can use it
        core = Core(d, Settings(d), self.tmp.name)
        core.nodes.add(1, "Stará nádrž")
        core.aggregate_hours(since=0)
        self.assertEqual(d.scalar("SELECT COUNT(*) FROM hourly WHERE node_id=1"), 2)

    def test_every_intermediate_version_migrates(self):
        """A hub can be updated from any older release, not only from v1."""
        fresh_schema = None
        for start in range(1, len(MIGRATIONS)):
            path = os.path.join(self.tmp.name, f"v{start}.db")
            con = sqlite3.connect(path)
            for i in range(start):
                for stmt in db_mod._split_sql(MIGRATIONS[i]):
                    con.execute(stmt)
            con.execute(f"PRAGMA user_version={start}")
            con.commit()
            con.close()
            d = self.open(path)
            self.assertEqual(d.scalar("PRAGMA user_version"), len(MIGRATIONS), start)
            if fresh_schema is None:
                self.open(self.fresh)
                fresh_schema = schema(self.fresh)
            self.assertEqual(schema(path), fresh_schema, f"from v{start}")

    def test_rewound_version_is_tolerated(self):
        """A DB whose user_version was set back (restored by hand) re-runs ADD COLUMNs."""
        d = self.open(self.fresh)
        d.close_thread()
        self.opened.remove(d)
        con = sqlite3.connect(self.fresh)
        con.execute("PRAGMA user_version=11")
        con.close()
        d = self.open(self.fresh)
        self.assertEqual(d.scalar("PRAGMA user_version"), len(MIGRATIONS))

    def test_failed_migration_rolls_back_and_keeps_the_version(self):
        make_v1(self.old)
        saved = list(MIGRATIONS)
        MIGRATIONS.append("CREATE TABLE broken_mig(a); INSERT INTO no_such_table VALUES(1)")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                Database(self.old).conn()
        finally:
            MIGRATIONS[:] = saved
        con = sqlite3.connect(self.old)
        try:
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], len(saved))
            self.assertIsNone(con.execute("SELECT 1 FROM sqlite_master WHERE name='broken_mig'").fetchone())
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()

"""Backup + restore: download / nightly copy -> import into a fresh hub, merge rules, bad files."""
import gzip
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from retencka import core as core_mod  # noqa: E402
from retencka.core import Core  # noqa: E402
from retencka.db import Database  # noqa: E402
from retencka.settings import Settings  # noqa: E402

A = 0x0A01
T0 = 1_760_000_400 - 1_760_000_400 % 3600
K_ENC, K_MAC = bytes(range(16)), bytes(range(16, 32))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dbs = []

    def tearDown(self):
        for db in self.dbs:
            db.close_thread()
        self.tmp.cleanup()

    def hub(self, name: str) -> Core:
        d = os.path.join(self.tmp.name, name)
        os.makedirs(d)
        db = Database(os.path.join(d, "retencka.db"))
        self.dbs.append(db)
        return Core(db, Settings(db), d)

    def source_hub(self) -> Core:
        c = self.hub("old")
        c.nodes.add(A, "Zahrada")
        c.nodes.set_keys(A, K_ENC, K_MAC)
        with c.db.transaction() as x:
            for i in range(48):
                x.execute("INSERT INTO measurements(ts, node_id, seq, flags, level_mm, volume_l) VALUES(?,?,?,0,?,?)",
                          (T0 + i * 1800, A, i, 500 + i, 5000 + i))
            x.execute("INSERT INTO refills(start_ts, end_ts, node_id, mode, target_pct) VALUES(?,?,?,'manual',50)",
                      (T0 + 3600, T0 + 4000, A))
            x.execute("INSERT INTO users(username, pw_hash, role, created, pw_changed) VALUES('pepa','h','admin',1,1)")
        c.settings.update({"mqtt_uid": "tok-from-backup"}, allow_internal=True)
        c._save_ctr(A, 47)                              # as the receiver does for every frame
        c.aggregate_hours(since=0)
        c.audit("LOGIN_SUCCESS", "v záloze", "pepa")
        return c

    def gz_snapshot(self, c: Core) -> str:
        """Exactly what the web download / the nightly copy produce: gzip of a snapshot."""
        raw = os.path.join(self.tmp.name, "snap.db")
        c.snapshot(raw)
        out = raw + ".gz"
        with open(raw, "rb") as fi, gzip.open(out, "wb") as fo:
            fo.write(fi.read())
        os.remove(raw)
        return out


class RestoreTest(Base):
    def test_full_restore_into_a_fresh_hub(self):
        snap = self.gz_snapshot(self.source_hub())
        new = self.hub("new")
        st = new.import_backup(snap, full=True)
        self.assertEqual((st["measurements"], st["skipped"], st["nodes"], st["refills"], st["users"]), (48, 0, 1, 1, 1))
        self.assertTrue(st["settings"])
        self.assertEqual(new.nodes.keys(A), (K_ENC, K_MAC))           # paired sensor keeps working
        self.assertEqual(new.nodes.get(A)["name"], "Zahrada")
        self.assertEqual(new.settings.get("mqtt_uid"), "tok-from-backup")
        self.assertEqual(new.db.scalar("SELECT role FROM users WHERE username='pepa'"), "admin")
        self.assertEqual(new.db.scalar("SELECT COUNT(*) FROM hourly WHERE node_id=?", (A,)), 24)
        self.assertEqual(new._last_ctr[A], 47)                         # replays of old frames refused
        self.assertIsNotNone(new.db.one("SELECT 1 FROM audit WHERE detail='v záloze'"))
        # importing the same file again changes nothing
        again = new.import_backup(snap, full=True)
        self.assertEqual((again["measurements"], again["skipped"], again["nodes"], again["users"]), (0, 48, 0, 0))
        self.assertEqual(new.db.scalar("SELECT COUNT(*) FROM measurements"), 48)
        # nothing left behind in the backup directory
        self.assertEqual(os.listdir(new.backup_dir()), [])

    def test_data_only_import_keeps_users_and_settings(self):
        snap = self.gz_snapshot(self.source_hub())
        new = self.hub("new")
        new.settings.update({"mqtt_uid": "mine"}, allow_internal=True)
        st = new.import_backup(snap)
        self.assertEqual(st["measurements"], 48)
        self.assertFalse(st["settings"])
        self.assertEqual(new.settings.get("mqtt_uid"), "mine")
        self.assertIsNone(new.db.one("SELECT 1 FROM users WHERE username='pepa'"))

    def test_nightly_local_copy_restores(self):
        src = self.source_hub()
        path = src.backup_local()
        self.assertEqual([b["name"] for b in src.backups()], [os.path.basename(path)])
        new = self.hub("new")
        self.assertEqual(new.import_backup(path)["measurements"], 48)

    def test_uncompressed_db_also_accepted(self):
        src = self.source_hub()
        raw = os.path.join(self.tmp.name, "plain.db")
        src.snapshot(raw)
        self.assertEqual(self.hub("new").import_backup(raw)["measurements"], 48)


class BadFileTest(Base):
    def test_garbage_and_foreign_db_refused(self):
        new = self.hub("new")
        junk = os.path.join(self.tmp.name, "junk.bin")
        with open(junk, "wb") as f:
            f.write(os.urandom(4096))
        with self.assertRaises(ValueError):
            new.import_backup(junk)
        foreign = os.path.join(self.tmp.name, "foreign.db")
        con = sqlite3.connect(foreign)
        con.execute("CREATE TABLE x(a)")
        con.commit()
        con.close()
        with self.assertRaises(ValueError):
            new.import_backup(foreign)
        broken = os.path.join(self.tmp.name, "broken.gz")
        with open(broken, "wb") as f:
            f.write(b"\x1f\x8b" + os.urandom(200))
        with self.assertRaises(ValueError):
            new.import_backup(broken)
        good = gzip.compress(b"x" * 100000)
        with open(broken, "wb") as f:                  # valid header, damaged deflate stream
            f.write(good[:20] + bytes(50) + good[70:])
        with self.assertRaises(ValueError):
            new.import_backup(broken)
        self.assertEqual(os.listdir(new.backup_dir()), [])

    def test_backup_from_newer_version_refused(self):
        src = self.source_hub()
        raw = os.path.join(self.tmp.name, "newer.db")
        src.snapshot(raw)
        con = sqlite3.connect(raw)
        con.execute("PRAGMA user_version=999")
        con.commit()
        con.close()
        with self.assertRaises(ValueError):
            self.hub("new").import_backup(raw)

    def test_gzip_bomb_refused_without_filling_the_card(self):
        """A small .gz that expands to far more than any real backup is refused while
        decompressing; the partial file is removed."""
        new = self.hub("new")
        bomb = os.path.join(self.tmp.name, "bomb.db.gz")
        with gzip.open(bomb, "wb", compresslevel=9) as f:
            chunk = bytes(1 << 20)
            for _ in range(8):                         # 8 MiB of zeros -> ~8 KiB file
                f.write(chunk)
        saved = core_mod.IMPORT_MAX_BYTES
        core_mod.IMPORT_MAX_BYTES = 2 << 20
        try:
            with self.assertRaises(ValueError) as cm:
                new.import_backup(bomb)
        finally:
            core_mod.IMPORT_MAX_BYTES = saved
        self.assertIn("velk", str(cm.exception))
        self.assertEqual(os.listdir(new.backup_dir()), [])


if __name__ == "__main__":
    unittest.main()

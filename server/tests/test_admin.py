"""Factory reset, user administration, audit of update results."""
import asyncio
import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from retencka import auth, reset, updates  # noqa: E402
from retencka.core import Core  # noqa: E402
from retencka.db import Database  # noqa: E402
from retencka.httpserver import Server  # noqa: E402
from retencka.settings import Settings  # noqa: E402
from retencka.web import build_web_app  # noqa: E402

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class UpdateAudit(unittest.TestCase):
    def test_audit_event_dedup(self):
        now = time.time()
        av = {"state": "available", "latest": "1.7.0", "message": "K dispozici je verze 1.7.0.",
              "updated_at": now, "trigger": "timer"}
        k, action, detail = updates.audit_event(av, None, now)
        self.assertEqual((action, detail), ("UPDATE_AVAILABLE", "K dispozici je verze 1.7.0."))
        self.assertIsNone(updates.audit_event({**av, "updated_at": now + 86400}, k, now))   # daily re-check
        self.assertIsNone(updates.audit_event({"state": "checking"}, k, now))
        err = {"state": "error", "message": "Kontrola aktualizací selhala: offline", "updated_at": now}
        k2, action, _ = updates.audit_event(err, k, now)
        self.assertEqual(action, "UPDATE_ERROR")
        self.assertIsNone(updates.audit_event({**err, "updated_at": now + 86400}, k2, now))
        done = {"state": "done", "target": "1.7.0", "message": "Aktualizováno 1.6.0 → 1.7.0", "updated_at": now}
        self.assertEqual(updates.audit_event(done, k2, now)[1], "UPDATE_INSTALLED")
        rb = {"state": "rolled_back", "target": "1.7.0", "message": "vráceno", "updated_at": now}
        self.assertEqual(updates.audit_event(rb, k2, now)[1], "UPDATE_ROLLED_BACK")
        # up to date: only after a manual check
        utd = {"state": "up_to_date", "latest": "1.6.0", "message": "Máte nejnovější verzi", "updated_at": now}
        self.assertIsNone(updates.audit_event({**utd, "trigger": "timer"}, k2, now))
        self.assertEqual(updates.audit_event({**utd, "trigger": "web"}, k2, now)[1], "UPDATE_UP_TO_DATE")
        # first run (nothing remembered): an old result is not logged as if it just happened
        self.assertIsNone(updates.audit_event({**err, "updated_at": now - 7200}, None, now))

    def test_core_tick_writes_audit_once(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            db = Database(os.path.join(d, "t.db"))
            core = Core(db, Settings(db), d)
            with open(os.path.join(d, updates.STATUS), "w", encoding="utf-8") as f:
                json.dump({"state": "done", "target": "1.7.0", "message": "Aktualizováno 1.6.0 → 1.7.0",
                           "updated_at": int(time.time()), "trigger": "web"}, f)
            core.update_tick()
            core.update_tick()
            rows = db.query("SELECT action, detail FROM audit WHERE action LIKE 'UPDATE_%'")
            self.assertEqual([tuple(r) for r in rows], [("UPDATE_INSTALLED", "Aktualizováno 1.6.0 → 1.7.0")])
            db.close_thread()


# =================================================================== factory reset
class FactoryResetFiles(unittest.TestCase):
    def test_apply_wipes_all_but_identity(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("retencka.db", "retencka.db-wal", "servis.json", "update-prefs.json", "setup_code"):
                Path(d, name).write_text("x")
            Path(d, "backup").mkdir()
            Path(d, "backup", "retencka-2026-10-01.db.gz").write_text("x")
            self.assertIsNone(reset.apply(d))                      # nothing requested
            self.assertTrue(Path(d, "retencka.db").exists())
            reset.request(d, "petr")
            info = reset.apply(d)
            self.assertEqual(info["user"], "petr")
            self.assertEqual(sorted(os.listdir(d)), ["setup_code"])

    def test_full_cycle_back_to_first_login(self):
        """Users, sensor keys and data are gone after the next start; the label password
        (setup_code) still opens the first login."""
        from retencka.db import Database
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            Path(d, "setup_code").write_text("Abcdefgh23\n")
            db = Database(os.path.join(d, "retencka.db"))
            db.execute("INSERT INTO users(username, pw_hash, role, created, pw_changed) VALUES('petr','h','admin',1,1)")
            db.execute("INSERT INTO nodes(node_id, name, config, created, secret) VALUES(1, 'T', '{}', 1, 'ab')")
            db.execute("INSERT INTO measurements(ts, node_id, seq, flags) VALUES(1, 1, 1, 0)")
            reset.request(d, "petr")
            db.close_thread()
            self.assertEqual(reset.apply(d)["user"], "petr")
            self.assertFalse(reset.pending(d))
            db = Database(os.path.join(d, "retencka.db"))
            try:
                for table in ("users", "nodes", "measurements", "sessions", "settings"):
                    self.assertEqual(db.scalar(f"SELECT COUNT(*) FROM {table}"), 0, table)
            finally:
                db.close_thread()
            self.assertEqual(Path(d, "setup_code").read_text(), "Abcdefgh23\n")
            self.assertIsNone(reset.apply(d))                      # runs only once

    def test_damaged_marker_still_wipes(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "retencka.db").write_text("x")
            Path(d, reset.MARKER).write_text("{not json")
            self.assertEqual(reset.apply(d)["user"], "?")
            self.assertEqual(os.listdir(d), [])

    def test_symlinked_entry_is_removed_not_followed(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as outside:
            Path(outside, "keep.txt").write_text("x")
            try:
                os.symlink(outside, os.path.join(d, "link"), target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks not available")
            reset.request(d, "petr")
            reset.apply(d)
            self.assertEqual(os.listdir(d), [])
            self.assertTrue(Path(outside, "keep.txt").exists())


# ============================================================ end-to-end over HTTP(S)
class AdminServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.port = free_port()
        cls.restarts = []
        ready = threading.Event()

        def run():
            loop = asyncio.new_event_loop()
            cls.loop = loop
            asyncio.set_event_loop(loop)

            async def start():
                db = Database(os.path.join(cls.tmp.name, "t.db"))
                cls.db = db
                cls.core = Core(db, Settings(db), cls.tmp.name)
                cls.core.loop = loop
                Path(cls.tmp.name, "setup_code").write_text("Label-Pass9\n")
                now = int(time.time())
                for u, role in (("boss", "admin"), ("eva", "user")):
                    db.execute("INSERT INTO users(username, pw_hash, role, created, pw_changed) VALUES(?,?,?,?,?)",
                               (u, auth.hash_password("Velmi-Tajne-Heslo1"), role, now, now))
                app = build_web_app(cls.core, os.path.join(os.path.dirname(__file__), "..", "static"))
                app["ha_port"] = 0
                app["restart"] = lambda: cls.restarts.append(time.time())
                cls.app = app
                await Server(app).start("127.0.0.1", cls.port)
                ready.set()
            loop.run_until_complete(start())
            loop.run_forever()

        threading.Thread(target=run, daemon=True).start()
        ready.wait(10)

    def req(self, method, path, body=None, headers=None, conn=None):
        c = conn or http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        h = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            js = json.loads(raw)
        except ValueError:
            js = None
        return r.status, js, r, raw

    def login(self, user="boss", conn=None):
        st, js, r, _ = self.req("POST", "/api/auth/login", {"username": user, "password": "Velmi-Tajne-Heslo1"}, conn=conn)
        self.assertEqual(st, 200, js)
        cookie = r.getheader("Set-Cookie").split(";")[0]
        st, me, _, _ = self.req("GET", "/api/me", headers={"Cookie": cookie})
        return {"Cookie": cookie, "X-CSRF-Token": me["csrf"]}

    def test_1_user_admin(self):
        h = self.login()
        st, js, _, _ = self.req("POST", "/api/users/role", {"username": "eva", "role": "admin"}, h)
        self.assertEqual(st, 200, js)
        st, js, _, _ = self.req("POST", "/api/users/password", {"username": "eva", "password": "Jine-Dlouhe-Heslo2"}, h)
        self.assertEqual(st, 200, js)
        st, js, r, _ = self.req("POST", "/api/auth/login", {"username": "eva", "password": "Jine-Dlouhe-Heslo2"})
        self.assertEqual(st, 200, js)
        cookie = r.getheader("Set-Cookie").split(";")[0]
        _, me, _, _ = self.req("GET", "/api/me", headers={"Cookie": cookie})
        h_eva = {"Cookie": cookie, "X-CSRF-Token": me["csrf"]}
        # two admins: eva may demote boss; then she is the last one and her own account is off limits
        st, js, _, _ = self.req("POST", "/api/users/role", {"username": "boss", "role": "user"}, h_eva)
        self.assertEqual(st, 200, js)
        st, _, _, _ = self.req("POST", "/api/users/role", {"username": "eva", "role": "user"}, h)
        self.assertEqual(st, 403)                  # boss's role is read live – no longer admin
        st, _, _, _ = self.req("POST", "/api/users/role", {"username": "eva", "role": "user"}, h_eva)
        self.assertEqual(st, 400)
        st, _, _, _ = self.req("POST", "/api/users/delete", {"username": "eva"}, h_eva)
        self.assertEqual(st, 400)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM users WHERE role='admin'"), 1)
        st, js, _, _ = self.req("POST", "/api/users/role", {"username": "boss", "role": "admin"}, h_eva)
        self.assertEqual(st, 200, js)
        actions = [r[0] for r in self.db.query("SELECT action FROM audit")]
        self.assertIn("USER_ROLE_CHANGED", actions)
        self.assertIn("PASSWORD_CHANGED", actions)

    def test_3_state_disk_and_backup_info(self):
        h = self.login()
        st, js, _, _ = self.req("GET", "/api/state", headers=h)
        self.assertEqual(st, 200)
        self.assertIn("disk_low", js)
        st, js, _, _ = self.req("GET", "/api/backup", headers=h)
        self.assertEqual(js["keep"], 7)
        self.assertTrue(js["dir"].endswith("backup"))

    def test_9_factory_reset(self):
        h = self.login()
        st, js, _, _ = self.req("POST", "/api/system/factory-reset", {"password": "Velmi-Tajne-Heslo1", "confirm": "ano"}, h)
        self.assertEqual(st, 400)
        st, js, _, _ = self.req("POST", "/api/system/factory-reset", {"password": "spatne", "confirm": "OBNOVIT"}, h)
        self.assertEqual(st, 403)
        self.assertFalse(reset.pending(self.tmp.name))
        st, js, _, _ = self.req("POST", "/api/system/factory-reset", {"password": "Velmi-Tajne-Heslo1", "confirm": "OBNOVIT"}, h)
        self.assertEqual(st, 200, js)
        self.assertTrue(reset.pending(self.tmp.name))
        time.sleep(1.8)
        self.assertEqual(len(self.restarts), 1)
        st, _, _, _ = self.req("GET", "/api/me", headers={"Cookie": h["Cookie"]})
        self.assertEqual(st, 200)                   # the wipe happens at the next start, not now


if __name__ == "__main__":
    unittest.main()

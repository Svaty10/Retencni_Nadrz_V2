"""Usernames are one identity regardless of case – also for non-ASCII letters (SQLite
NOCASE folds only A–Z) – and the stored spelling is what sessions and the audit show."""
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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from retencka import auth  # noqa: E402
from retencka.core import Core  # noqa: E402
from retencka.db import Database  # noqa: E402
from retencka.httpserver import Server  # noqa: E402
from retencka.settings import Settings  # noqa: E402
from retencka.web import build_web_app  # noqa: E402

PW = "Velmi-Tajne-Heslo1"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class LockKeyTest(unittest.TestCase):
    def test_lockout_key_ignores_case_also_for_accented_letters(self):
        locks = auth.LoginLocks(max_fails=3)
        for name in ("MATĚJ", "Matěj", "matěj"):
            locks.fail(name, "10.0.0.1")
        self.assertTrue(locks.locked("mAtĚj", "10.0.0.1"))
        self.assertFalse(locks.locked("matěj", "10.0.0.2"))

    def test_name_key(self):
        self.assertEqual(auth.name_key("MATĚJ"), auth.name_key("matěj"))
        self.assertEqual(auth.name_key("Matěj"), auth.name_key("Matěj"))      # NFD = NFC
        self.assertNotEqual(auth.name_key("Matej"), auth.name_key("Matěj"))


class UsernameServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.port = free_port()
        ready = threading.Event()

        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def start():
                db = Database(os.path.join(cls.tmp.name, "t.db"))
                cls.db = db
                core = Core(db, Settings(db), cls.tmp.name)
                core.loop = loop
                now = int(time.time())
                for u, role in (("Šéf", "admin"), ("Matěj", "user")):
                    db.execute("INSERT INTO users(username, pw_hash, role, created, pw_changed) VALUES(?,?,?,?,?)",
                               (u, auth.hash_password(PW), role, now, now))
                app = build_web_app(core, os.path.join(os.path.dirname(__file__), "..", "static"))
                await Server(app).start("127.0.0.1", cls.port)
                ready.set()
            loop.run_until_complete(start())
            loop.run_forever()

        threading.Thread(target=run, daemon=True).start()
        ready.wait(10)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        h = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, json.loads(raw), r

    def login(self, user):
        st, js, r = self.req("POST", "/api/auth/login", {"username": user, "password": PW})
        self.assertEqual(st, 200, (user, js))
        cookie = r.getheader("Set-Cookie").split(";")[0]
        _, me, _ = self.req("GET", "/api/me", headers={"Cookie": cookie})
        return me, {"Cookie": cookie, "X-CSRF-Token": me["csrf"]}

    def test_login_in_any_case_uses_the_stored_name(self):
        for typed in ("matěj", "MATĚJ", "Matěj"):
            me, _ = self.login(typed)
            self.assertEqual(me["username"], "Matěj", typed)
        rows = self.db.query("SELECT DISTINCT username FROM audit WHERE action='LOGIN_SUCCESS' "
                             "AND username LIKE 'Mat%'")
        self.assertEqual([r[0] for r in rows], ["Matěj"])

    def test_admin_actions_find_the_user_in_any_case(self):
        _, h = self.login("šÉF")
        st, js, _ = self.req("POST", "/api/users/add", {"username": "MATĚJ", "password": "Jine-Dlouhe-Heslo2"}, h)
        self.assertEqual(st, 400, js)                       # same person, refused
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM users"), 2)
        st, js, _ = self.req("POST", "/api/users/role", {"username": "MATĚJ", "role": "user"}, h)
        self.assertEqual(st, 200, js)
        st, js, _ = self.req("POST", "/api/users/role", {"username": "ŠÉF", "role": "user"}, h)
        self.assertEqual(st, 400, js)                       # own account, whatever the case
        self.assertEqual(self.db.scalar("SELECT role FROM users WHERE username='Šéf'"), "admin")


if __name__ == "__main__":
    unittest.main()

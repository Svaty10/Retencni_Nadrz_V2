"""HTTPS with the hub's own CA, factory reset, user administration, audit of update results."""
import asyncio
import http.client
import importlib.util
import json
import os
import shutil
import socket
import ssl
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
from retencka.tls import HttpsManager, TlsFiles  # noqa: E402
from retencka.web import build_web_app  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "system" / "bin"))
import retencka_tls as rtls  # noqa: E402

_spec = importlib.util.spec_from_file_location("retencka_control", ROOT / "system" / "bin" / "retencka-control.py")
control = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(control)               # the root helper imports retencka_tls fine

HAVE_OPENSSL = shutil.which("openssl") is not None
POSIX_FD = hasattr(os, "O_DIRECTORY") and hasattr(os, "getuid")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def make_cert_dir(target: str, names=("retencka.local", "retencka-ab12.local"), ips=("192.168.1.20",)):
    """What retencka_tls publishes, generated with the real openssl commands."""
    work = Path(tempfile.mkdtemp())
    try:
        rtls.make_ca(work, "retencka-ab12")
        rtls.make_server(work, list(names), list(ips))
        os.makedirs(target, exist_ok=True)
        for n in ("ca.crt", "server.crt", "server.key"):
            shutil.copy(work / n, os.path.join(target, n))
        status = {"fingerprint": rtls.fingerprint((work / "ca.crt").read_bytes()),
                  "ca_not_after": rtls.not_after(work / "ca.crt"), "cert_not_after": rtls.not_after(work / "server.crt"),
                  "names": list(names), "ips": list(ips), "issued_at": int(time.time()), "reason": "nový certifikát"}
        with open(os.path.join(target, "status.json"), "w") as f:
            json.dump(status, f)
        return status
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ====================================================================== TLS logic
class TlsLogic(unittest.TestCase):
    def test_names_and_ips(self):
        self.assertEqual(rtls.wanted_names("Retencka-AB12"), ["retencka.local", "retencka-ab12.local"])
        self.assertEqual(rtls.wanted_names("retencka"), ["retencka.local"])                # alias only once
        self.assertEqual(rtls.wanted_names("bad name;"), ["retencka.local"])
        text = ("1: lo    inet 127.0.0.1/8 scope host lo\n"
                "3: wlan0    inet 192.168.1.20/24 brd 192.168.1.255 scope global wlan0\n"
                "4: wt0    inet 100.92.3.4/16 scope global wt0\n"
                "5: eth0    inet 85.1.2.3/24 scope global eth0\n"
                "6: wlan0    inet 10.0.0.5/8 scope global wlan0\n")
        # public and loopback addresses never go into the certificate (CA name constraints)
        self.assertEqual(rtls.parse_ip_addr(text), ["10.0.0.5", "100.92.3.4", "192.168.1.20"])

    def test_renew_reason(self):
        now = 1_800_000_000
        ok = {"cert_not_after": now + 200 * 86400, "names": ["retencka.local", "a.local"], "ips": ["192.168.1.2"]}
        names, ips = ["retencka.local", "a.local"], ["192.168.1.2"]
        self.assertIsNone(rtls.renew_reason(ok, names, ips, now))
        self.assertIsNone(rtls.renew_reason(ok, names, [], now))           # offline: fewer IPs is fine
        self.assertEqual(rtls.renew_reason(None, names, ips, now), "nový certifikát")
        self.assertEqual(rtls.renew_reason({**ok, "cert_not_after": now + 29 * 86400}, names, ips, now),
                         "blíží se konec platnosti")
        self.assertEqual(rtls.renew_reason(ok, names, ["192.168.1.3"], now), "změna IP adresy")
        self.assertEqual(rtls.renew_reason(ok, ["retencka.local", "b.local"], ips, now), "změna názvu zařízení")

    def test_configs(self):
        ca = rtls.ca_config("retencka-ab12")
        self.assertIn("CA:TRUE, pathlen:0", ca)
        self.assertIn("nameConstraints = critical, permitted;DNS:local", ca)
        self.assertIn("permitted;IP:192.168.0.0/255.255.0.0", ca)
        srv = rtls.server_config(["retencka.local", "retencka-ab12.local"], ["192.168.1.20"])
        self.assertIn("DNS.2 = retencka-ab12.local", srv)
        self.assertIn("IP.1 = 192.168.1.20", srv)
        self.assertIn("extendedKeyUsage = serverAuth", srv)
        self.assertIn("CN = retencka-ab12.local", srv)

    @unittest.skipUnless(HAVE_OPENSSL, "openssl not installed")
    def test_openssl_chain_verifies(self):
        with tempfile.TemporaryDirectory() as d:
            st = make_cert_dir(d)
            self.assertEqual(len(st["fingerprint"].split(":")), 32)
            self.assertGreater(st["ca_not_after"] - time.time(), 3600 * 86400)
            self.assertTrue(390 * 86400 < st["cert_not_after"] - time.time() < 400 * 86400)
            ctx = ssl.create_default_context(cafile=os.path.join(d, "ca.crt"))
            srv = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            srv.load_cert_chain(os.path.join(d, "server.crt"), os.path.join(d, "server.key"))
            self.assertTrue(ctx.check_hostname)

    @unittest.skipUnless(HAVE_OPENSSL and POSIX_FD, "needs openssl and POSIX dir fds")
    def test_ensure_creates_renews_and_publishes(self):
        with tempfile.TemporaryDirectory() as d:
            ca, pub = Path(d, "tls-ca"), Path(d, "data", "tls")
            pub.parent.mkdir()
            uid = os.getuid()
            self.assertEqual(rtls.ensure(ca, pub, "retencka-ab12", ["192.168.1.20"], uid=uid), "nový certifikát")
            fp = json.loads((pub / "status.json").read_text())["fingerprint"]
            self.assertEqual(oct((ca / "ca.key").stat().st_mode & 0o777), "0o600")
            self.assertFalse((pub / "ca.key").exists())
            self.assertIsNone(rtls.ensure(ca, pub, "retencka-ab12", ["192.168.1.20"], uid=uid))
            # new IP -> new server certificate, same CA
            self.assertEqual(rtls.ensure(ca, pub, "retencka-ab12", ["192.168.1.30"], uid=uid), "změna IP adresy")
            st = json.loads((pub / "status.json").read_text())
            self.assertEqual(st["fingerprint"], fp)
            self.assertEqual(st["ips"], ["192.168.1.30"])
            # a symlink planted in place of the published directory is moved aside, not followed
            shutil.rmtree(pub)
            os.symlink(d, pub)
            self.assertIsNone(rtls.ensure(ca, pub, "retencka-ab12", ["192.168.1.30"], uid=uid))
            self.assertFalse(pub.is_symlink())
            self.assertTrue((pub / "server.crt").is_file())


# ======================================================= update results -> audit
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
    def test_apply_wipes_all_but_identity_and_tls(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("retencka.db", "retencka.db-wal", "servis.json", "update-prefs.json", "setup_code"):
                Path(d, name).write_text("x")
            Path(d, "backup").mkdir()
            Path(d, "backup", "retencka-2026-10-01.db.gz").write_text("x")
            Path(d, "tls").mkdir()
            Path(d, "tls", "ca.crt").write_text("x")
            self.assertIsNone(reset.apply(d))                      # nothing requested
            self.assertTrue(Path(d, "retencka.db").exists())
            reset.request(d, "petr")
            info = reset.apply(d)
            self.assertEqual(info["user"], "petr")
            self.assertEqual(sorted(os.listdir(d)), ["setup_code", "tls"])
            self.assertTrue(Path(d, "tls", "ca.crt").exists())

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
        cls.port, cls.https_port = free_port(), free_port()
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
                cls.https = HttpsManager(cls.core, app, "127.0.0.1", cls.https_port)
                cls.core.https = cls.https
                await Server(app).start("127.0.0.1", cls.port)
                ready.set()
            loop.run_until_complete(start())
            loop.run_forever()

        threading.Thread(target=run, daemon=True).start()
        ready.wait(10)

    def tick(self):
        asyncio.run_coroutine_threadsafe(self.https.tick(), self.loop).result(10)

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

    def test_2_tls_endpoints_and_redirect(self):
        st, _, _, raw = self.req("GET", "/ca.crt")
        self.assertEqual(st, 404)
        h = self.login()
        st, js, _, _ = self.req("GET", "/api/tls", headers=h)
        self.assertEqual((st, js["available"], js["active"]), (200, False, False))
        if not HAVE_OPENSSL:
            self.skipTest("openssl not installed")
        self.tick()                                 # no certificate: asks the root helper
        self.assertTrue(Path(self.tmp.name, "control-tls").exists())
        make_cert_dir(os.path.join(self.tmp.name, "tls"), ips=("192.168.1.20",))
        self.tick()
        self.assertTrue(self.https.active)
        self.assertEqual(self.app.get("https_port"), self.https_port)
        actions = [r[0] for r in self.db.query("SELECT action FROM audit")]
        self.assertEqual(actions.count("TLS_CERT_ISSUED"), 1)
        # CA download over plain http, no login
        st, _, r, raw = self.req("GET", "/ca.crt")
        self.assertEqual(st, 200)
        self.assertTrue(raw.startswith(b"-----BEGIN CERTIFICATE-----"))
        self.assertEqual(r.getheader("Content-Type"), "application/x-x509-ca-cert")
        # loopback (cloudflared, tools) is not redirected; the real check of a LAN client:
        mw_redirect = self.app.middlewares[1]

        class FakeReq:
            secure, remote, path, target, method = False, "192.168.1.50", "/", "/?node=1", "GET"
            host = "retencka.local"
            app = self.app

        async def handler(_):
            return "handled"
        # off by default: http keeps working
        self.assertEqual(asyncio.run_coroutine_threadsafe(mw_redirect(FakeReq(), handler), self.loop).result(5), "handled")
        st, js, _, _ = self.req("POST", "/api/tls", {"redirect": True}, h)
        self.assertEqual(st, 400)                   # only from an https:// page
        self.core.settings.update({"https_redirect": True}, allow_internal=True)
        resp = asyncio.run_coroutine_threadsafe(mw_redirect(FakeReq(), handler), self.loop).result(5)
        self.assertEqual(resp.status, 302)
        self.assertEqual(resp.headers["Location"], f"https://retencka.local:{self.https_port}/?node=1")
        FakeReq.host = "evil.example\r\nX: y"
        self.assertEqual(asyncio.run_coroutine_threadsafe(mw_redirect(FakeReq(), handler), self.loop).result(5), "handled")
        FakeReq.host, FakeReq.path = "retencka.local", "/ca.crt"
        self.assertEqual(asyncio.run_coroutine_threadsafe(mw_redirect(FakeReq(), handler), self.loop).result(5), "handled")
        # real HTTPS: trusted via the hub CA, secure session cookie, no HSTS with the own CA
        ctx = ssl.create_default_context(cafile=os.path.join(self.tmp.name, "tls", "ca.crt"))
        sock = socket.create_connection(("127.0.0.1", self.https_port), timeout=10)
        conn = http.client.HTTPSConnection("retencka.local", self.https_port, context=ctx, timeout=15)
        conn.sock = ctx.wrap_socket(sock, server_hostname="retencka.local")
        conn.request("POST", "/api/auth/login", body=json.dumps({"username": "boss", "password": "Velmi-Tajne-Heslo1"}),
                     headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        r.read()
        self.assertEqual(r.status, 200)
        self.assertTrue(r.getheader("Set-Cookie").startswith("__Host-rt_sid="))
        self.assertIn("Secure", r.getheader("Set-Cookie"))
        self.assertIsNone(r.getheader("Strict-Transport-Security"))
        self.assertIn("geolocation=(self)", r.getheader("Permissions-Policy"))
        cookie = r.getheader("Set-Cookie").split(";")[0]
        conn.close()
        conn = http.client.HTTPSConnection("127.0.0.1", self.https_port, context=ctx, timeout=15)
        conn.sock = ctx.wrap_socket(socket.create_connection(("127.0.0.1", self.https_port), timeout=10),
                                    server_hostname="retencka.local")
        conn.request("GET", "/api/me", headers={"Cookie": cookie, "Host": "retencka.local"})
        csrf = json.loads(conn.getresponse().read())["csrf"]
        conn.request("POST", "/api/tls", body=json.dumps({"redirect": False}),
                     headers={"Cookie": cookie, "X-CSRF-Token": csrf, "Content-Type": "application/json",
                              "Host": "retencka.local"})
        r = conn.getresponse()
        self.assertEqual((r.status, json.loads(r.read())["redirect"]), (200, False))
        conn.request("POST", "/api/tls", body=json.dumps({"redirect": True}),
                     headers={"Cookie": cookie, "X-CSRF-Token": csrf, "Content-Type": "application/json",
                              "Host": "retencka.local"})
        r = conn.getresponse()
        self.assertEqual((r.status, json.loads(r.read())["redirect"]), (200, True))
        conn.close()
        st, js, _, _ = self.req("GET", "/api/tls", headers=h)
        self.assertTrue(js["available"] and js["active"])
        self.assertEqual(js["names"][0], "retencka.local")
        # renewal: a changed status.json restarts the listener with the new certificate
        old = self.https.server
        make_cert_dir(os.path.join(self.tmp.name, "tls"), ips=("192.168.1.21",))
        self.tick()
        self.assertIsNot(self.https.server, old)
        self.https.stop()
        self.app["https_port"] = None

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

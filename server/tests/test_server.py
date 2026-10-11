"""End-to-end: real HTTP server (mock radio) – auth, CSRF, roles, HA token, limits."""
import asyncio
import gzip
import http.client
import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from retencka import crypto  # noqa: E402
from retencka.core import Core  # noqa: E402
from retencka.db import Database  # noqa: E402
from retencka.httpserver import Server  # noqa: E402
from retencka.settings import Settings  # noqa: E402
from retencka.web import build_ha_app, build_web_app  # noqa: E402


class FakeNode:
    """Behaves like the firmware: a paired node accepts only an UNPAIR signed with its
    key, an unpaired node accepts a PAIR_ACK; then it sends encrypted frames."""

    def __init__(self, core, node_id):
        self.core, self.id = core, node_id
        self.keys = None
        self.ctr = 0
        self.reboot()

    def reboot(self):
        self.priv = os.urandom(32)              # fresh key pair after every power-up
        self.pub = crypto.x25519_base(self.priv)

    def try_pair(self):
        """One PAIR_REQ. Returns 'paired', 'unpaired' or None."""
        req = crypto.build_pair_req(self.id, self.pub, self.keys is not None)
        reply = self.core.responder(req)
        self.core.on_frame(req, -80.0, 20)
        if not reply:
            return None
        if self.keys is not None:
            if crypto.check_unpair(self.keys[1], self.id, self.pub, reply):
                self.keys = None
                return "unpaired"
            return None
        if len(reply) == crypto.PAIR_LEN and reply[0] == crypto.PKT_PAIR_ACK:
            self.keys = crypto.derive_keys(crypto.x25519(self.priv, reply[3:35]))
            return "paired"
        return None

    def send(self, probe_100uv, flags=0, ctr=None, keys=None):
        k = keys or self.keys
        c = self.ctr if ctr is None else ctr
        self.ctr = max(self.ctr, c + 1)
        frame = crypto.build_data(k[0], k[1], self.id, c, crypto.build_telemetry(flags, 4900, 1500, 8000, probe_100uv))
        self.core.on_frame(frame, -80.0, 20)
        return frame


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.web_port, cls.ha_port = free_port(), free_port()
        ready = threading.Event()

        def run():
            loop = asyncio.new_event_loop()
            cls.loop = loop
            asyncio.set_event_loop(loop)

            async def start():
                db = Database(os.path.join(cls.tmp.name, "t.db"))
                cls.core = Core(db, Settings(db), cls.tmp.name)
                cls.core.loop = loop
                with open(os.path.join(cls.tmp.name, "setup_code"), "w") as f:
                    f.write("Label-Pass9\n")
                web_app = build_web_app(cls.core, os.path.join(os.path.dirname(__file__), "..", "static"))
                web_app["ha_port"] = cls.ha_port
                await Server(web_app).start("127.0.0.1", cls.web_port)
                await Server(build_ha_app(cls.core)).start("127.0.0.1", cls.ha_port)
                ready.set()
            loop.run_until_complete(start())
            loop.run_forever()

        threading.Thread(target=run, daemon=True).start()
        ready.wait(10)

    def req(self, method, path, body=None, headers=None, port=None):
        c = http.client.HTTPConnection("127.0.0.1", port or self.web_port, timeout=15)
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
        return r.status, js, r

    def login(self):
        st, js, r = self.req("POST", "/api/auth/login", {"username": "admin2", "password": "Velmi-Tajne-Heslo1"})
        self.assertEqual(st, 200, js)
        cookie = r.getheader("Set-Cookie").split(";")[0]
        st, me, _ = self.req("GET", "/api/me", headers={"Cookie": cookie})
        return {"Cookie": cookie, "X-CSRF-Token": me["csrf"]}

    def test_1_setup_and_flow(self):
        st, js, _ = self.req("GET", "/api/state")
        self.assertEqual(st, 401)
        st, js, _ = self.req("GET", "/api/auth/mode")
        self.assertTrue(js["first_login"])
        # wrong default password
        st, js, _ = self.req("POST", "/api/auth/login", {"username": "admin", "password": "nope"})
        self.assertEqual(st, 401)
        # setup without ticket is refused
        st, js, _ = self.req("POST", "/api/auth/setup", {"username": "matej", "password": "Velmi-Tajne-Heslo1"})
        self.assertEqual(st, 403)
        # default account admin + label password -> setup ticket
        st, js, r = self.req("POST", "/api/auth/login", {"username": "admin", "password": "Label-Pass9"})
        self.assertEqual(st, 200, js)
        self.assertTrue(js["setup"])
        ticket = r.getheader("Set-Cookie").split(";")[0]
        st, js, _ = self.req("POST", "/api/auth/setup", {"username": "admin", "password": "Velmi-Tajne-Heslo1"},
                             headers={"Cookie": ticket})
        self.assertEqual(st, 400)                     # own name required
        st, js, _ = self.req("POST", "/api/auth/setup", {"username": "matej", "password": "Label-Pass9"},
                             headers={"Cookie": ticket})
        self.assertEqual(st, 400)                     # must differ from the label password
        st, js, _ = self.req("POST", "/api/auth/setup", {"username": "admin2", "password": "Velmi-Tajne-Heslo1"},
                             headers={"Cookie": ticket})
        self.assertEqual(st, 200, js)
        # default credentials are dead now
        st, js, _ = self.req("POST", "/api/auth/login", {"username": "admin", "password": "Label-Pass9"})
        self.assertEqual(st, 401)
        self.core.ip_limiter.success("127.0.0.1")

        h = self.login()
        # first node pairs automatically (no node registered yet)
        n1 = FakeNode(self.core, 0x1234)
        self.assertEqual(n1.try_pair(), "paired")
        n1.send(7000)
        time.sleep(0.3)
        st, js, _ = self.req("GET", "/api/state", headers=h)
        self.assertEqual(st, 200)
        self.assertEqual(js["status"], "online")
        self.assertEqual(js["node"]["id"], "1234")
        self.assertIsNotNone(js["water_level_cm"])

        # a second node is NOT auto-paired any more, forged / replayed frames are dropped
        n2 = FakeNode(self.core, 0x9999)
        self.assertIsNone(n2.try_pair())
        frame = n1.send(7100)
        self.core.on_frame(frame, -80.0, 20)                     # replay
        n1.send(1000, keys=(os.urandom(16), os.urandom(16)))     # forged
        time.sleep(0.3)
        st, js2, _ = self.req("GET", "/api/state", headers=h)
        self.assertEqual(js2["probe_mv"], 710.0)

        # CSRF
        st, js, _ = self.req("POST", "/api/node", {"max_depth_cm": 100}, headers={"Cookie": h["Cookie"]})
        self.assertEqual(st, 403)
        st, js, _ = self.req("POST", "/api/node", {"max_depth_cm": 100}, headers=h)
        self.assertEqual(st, 200, js)
        st, js, _ = self.req("POST", "/api/node", {"max_depth_cm": -5}, headers=h)
        self.assertEqual(st, 400)
        st, js, _ = self.req("POST", "/api/settings", {"hydrawise_api_key": "secret-key-1234"}, headers=h)
        self.assertNotIn("secret-key", json.dumps(js))
        # cross-origin
        st, js, _ = self.req("POST", "/api/node", {"max_depth_cm": 90},
                             headers={**h, "Origin": "http://evil.example"})
        self.assertEqual(st, 403)

        # second sensor: user confirms pairing of the ID from the label
        st, js, _ = self.req("POST", "/api/pairing/start", {"node_id": "9999", "name": "Jímka"}, headers=h)
        self.assertEqual(st, 200, js)
        self.assertEqual(n2.try_pair(), "paired")
        n2.send(1000)
        time.sleep(0.3)
        st, js3, _ = self.req("GET", "/api/state?node=9999", headers=h)
        self.assertEqual(js3["node"]["name"], "Jímka")
        self.assertEqual(js3["probe_mv"], 100.0)
        self.assertEqual(len(js3["nodes"]), 2)
        st, js, _ = self.req("POST", "/api/node?node=9999", {"probe_accuracy_pct": 0.2, "probe_range_cm": 200},
                             headers=h)
        self.assertEqual(js["node"]["deadband_cm"], 0.4)
        st, js, _ = self.req("POST", "/api/pairing/start", {"node_id": "XYZ"}, headers=h)
        self.assertEqual(st, 400)

        # a paired node ignores pairing unless its own hub asks (battery re-inserted)
        n1.reboot()
        self.assertIsNone(n1.try_pair())
        # re-pairing node 1 = UNPAIR signed with the old key, then a new key
        old = n1.keys
        st, js, _ = self.req("POST", "/api/pairing/start", {"node_id": "1234"}, headers=h)
        self.assertEqual(n1.try_pair(), "unpaired")
        self.assertEqual(n1.try_pair(), "paired")
        self.assertNotEqual(n1.keys, old)
        n1.send(7300)
        time.sleep(0.3)
        n1.send(7400, keys=old)                                  # old key now rejected
        time.sleep(0.3)
        st, js4, _ = self.req("GET", "/api/state?node=1234", headers=h)
        self.assertEqual(js4["probe_mv"], 730.0)

        # unpairing node 2: hub stops accepting data at once, the key is deleted only
        # after the sensor confirms (battery re-inserted -> UNPAIR -> unpaired request)
        st, js, _ = self.req("POST", "/api/nodes/remove", {"node_id": "9999"}, headers=h)
        self.assertEqual(st, 200, js)
        self.assertFalse(js["removed"])
        n2.send(2000)
        time.sleep(0.3)
        st, js, _ = self.req("GET", "/api/nodes", headers=h)
        self.assertTrue([n for n in js["nodes"] if n["id"] == "9999"][0]["unpairing"])
        self.assertEqual(self.core.last.get(0x9999), None)
        n2.reboot()
        self.assertEqual(n2.try_pair(), "unpaired")
        self.assertIsNone(n2.try_pair())                         # confirmation -> removed
        time.sleep(0.3)
        st, js, _ = self.req("GET", "/api/nodes", headers=h)
        self.assertEqual([n["id"] for n in js["nodes"]], ["1234"])

        # two tanks with the same Hydrawise valve: only the first one drives it
        self.core.nodes.update(0x1234, {"refill_provider": "hydrawise", "hydrawise_relay_id": "555",
                                        "hydrawise_api_key": "acct-key"})
        self.core.nodes.add(0x2222, "Druhá")
        self.core.nodes.update(0x2222, {"refill_provider": "hydrawise", "hydrawise_relay_id": " 555",
                                        "hydrawise_api_key": "acct-key"})
        self.assertEqual(self.core.zone_owner(self.core.nodes.get(0x2222)), 0x1234)
        self.assertIsNotNone(self.core._provider(self.core.nodes.get(0x1234)))
        self.assertIsNone(self.core._provider(self.core.nodes.get(0x2222)))
        self.assertIsNotNone(self.core._provider(self.core.nodes.get(0x2222), for_stop=True))
        info = self.core.refill_info(0x2222)
        self.assertFalse(info["configured"])
        self.assertEqual(info["zone_controlled_by"]["id"], "1234")
        ok, msg = asyncio.run_coroutine_threadsafe(
            self.core.refill_start(0x2222, 80, "manual", "t"), self.loop).result(5)
        self.assertFalse(ok)
        self.assertIn("ovládá", msg)
        self.core.nodes.update(0x2222, {"hydrawise_relay_id": "556"})      # other zone -> independent
        self.assertIsNotNone(self.core._provider(self.core.nodes.get(0x2222)))
        self.core.nodes.remove(0x2222)

        # history carries events, signal and rain series
        st, js, _ = self.req("GET", "/api/history?node=1234&range=24h", headers=h)
        self.assertTrue(all(b["kind"] in ("refill", "rain") for b in js["bands"]))
        self.assertIsInstance(js["boots"], list)
        self.assertIn("rssi", js)

        # backup download = valid gzip'd SQLite database
        c = http.client.HTTPConnection("127.0.0.1", self.web_port, timeout=15)
        c.request("GET", "/api/backup/download", headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        self.assertEqual(r.status, 200)
        dbfile = os.path.join(self.tmp.name, "restored.db")
        with open(dbfile, "wb") as f:
            f.write(gzip.decompress(raw))
        con = sqlite3.connect(dbfile)
        self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertGreater(con.execute("SELECT COUNT(*) FROM measurements").fetchone()[0], 0)
        con.close()
        # import: the same backup again adds nothing (duplicates skipped)
        c = http.client.HTTPConnection("127.0.0.1", self.web_port, timeout=30)
        c.request("POST", "/api/backup/import", body=raw, headers={**h, "Content-Type": "application/gzip"})
        r = c.getresponse()
        js = json.loads(r.read())
        c.close()
        self.assertEqual(r.status, 200, js)
        self.assertEqual(js["measurements"], 0)
        self.assertGreater(js["skipped"], 0)
        # after deleting some rows, the import brings exactly them back
        n_before = self.core.db.scalar("SELECT COUNT(*) FROM measurements")
        self.core.db.execute("DELETE FROM measurements WHERE id IN (SELECT id FROM measurements LIMIT 2)")
        c = http.client.HTTPConnection("127.0.0.1", self.web_port, timeout=30)
        c.request("POST", "/api/backup/import?full=1", body=raw, headers=h)
        r = c.getresponse()
        js = json.loads(r.read())
        c.close()
        self.assertEqual(js["measurements"], 2, js)
        self.assertEqual(self.core.db.scalar("SELECT COUNT(*) FROM measurements"), n_before)
        c = http.client.HTTPConnection("127.0.0.1", self.web_port, timeout=30)
        c.request("POST", "/api/backup/import", body=b"x" * 5000, headers=h)
        r = c.getresponse()
        self.assertEqual(r.status, 400)
        r.read()
        c.close()
        path = self.core.backup_local()
        self.assertTrue(os.path.exists(path))
        st, js, _ = self.req("GET", "/api/backup", headers=h)
        self.assertEqual(len(js["backups"]), 1)

        # HA token
        st, js, _ = self.req("GET", "/api/v1/water/state", port=self.ha_port)
        self.assertEqual(st, 401)
        token = self.core.settings.get("ha_token")
        st, js, _ = self.req("GET", "/api/v1/water/state", headers={"Authorization": "Bearer " + token},
                             port=self.ha_port)
        self.assertEqual(st, 200)
        self.assertEqual(js["status"], "online")

        # user role cannot change settings
        st, js, _ = self.req("POST", "/api/users/add",
                             {"username": "host", "password": "Jine-Heslo-123", "role": "user"}, headers=h)
        self.assertEqual(st, 200, js)
        st, js, r = self.req("POST", "/api/auth/login", {"username": "host", "password": "Jine-Heslo-123"})
        hc = r.getheader("Set-Cookie").split(";")[0]
        _, me, _ = self.req("GET", "/api/me", headers={"Cookie": hc})
        st, js, _ = self.req("POST", "/api/node", {"max_depth_cm": 80},
                             headers={"Cookie": hc, "X-CSRF-Token": me["csrf"]})
        self.assertEqual(st, 403)
        st, js, _ = self.req("GET", "/api/audit", headers={"Cookie": hc})
        self.assertEqual(st, 403)
        st, js, _ = self.req("GET", "/api/backup", headers={"Cookie": hc})
        self.assertEqual(st, 403)

        # history + csv
        st, js, _ = self.req("GET", "/api/history?range=24h", headers=h)
        self.assertEqual(st, 200)
        self.assertTrue(js["t"])
        now = int(time.time())
        st, js, _ = self.req("GET", f"/api/history?from={now - 3600}&to={now + 60}", headers=h)
        self.assertEqual(st, 200)

    def test_2_bruteforce_and_limits(self):
        for _ in range(4):
            self.req("POST", "/api/auth/login", {"username": "nobody", "password": "wrong-password"})
        st, js, _ = self.req("POST", "/api/auth/login", {"username": "nobody", "password": "wrong-password"})
        self.assertEqual(st, 429)
        self.core.ip_limiter.success("127.0.0.1")
        # oversized body
        c = http.client.HTTPConnection("127.0.0.1", self.web_port)
        c.request("POST", "/api/auth/login", body=b"x" * 70000, headers={"Content-Type": "application/json"})
        self.assertEqual(c.getresponse().status, 413)
        # path traversal
        st, _, _ = self.req("GET", "/static/../retencka/auth.py")
        self.assertEqual(st, 404)
        st, _, _ = self.req("GET", "/static/%2e%2e/retencka/auth.py")
        self.assertEqual(st, 404)


if __name__ == "__main__":
    unittest.main()

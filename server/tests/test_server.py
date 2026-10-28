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
from retencka.web import build_web_app  # noqa: E402


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
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.web_port = free_port()
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
                cls.web_app = web_app
                await Server(web_app).start("127.0.0.1", cls.web_port)
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
        # pairing needs the user's confirmation, even for the very first sensor
        n1 = FakeNode(self.core, 0x1234)
        self.assertIsNone(n1.try_pair())
        st, js, _ = self.req("POST", "/api/pairing/start", {"node_id": "1234", "name": "Nádrž"}, headers=h)
        self.assertEqual(st, 200, js)
        self.assertEqual(n1.try_pair(), "paired")
        n1.send(7000)
        time.sleep(0.3)
        st, js, _ = self.req("GET", "/api/state", headers=h)
        self.assertEqual(st, 200)
        self.assertEqual(js["status"], "online")
        self.assertEqual(js["node"]["id"], "1234")
        self.assertIsNotNone(js["water_level_cm"])

        # a second node is not paired without confirmation, forged / replayed frames are dropped
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
        self.assertTrue(js["recalc"])                            # tank parameter -> history recomputed by itself
        for _ in range(50):
            if self.core.db.scalar("SELECT COUNT(*) FROM audit WHERE detail LIKE '%přepočítána automaticky%'", (), 0):
                break
            time.sleep(0.1)
        self.assertTrue(self.core.db.scalar("SELECT COUNT(*) FROM audit WHERE detail LIKE '%přepočítána automaticky%'", (), 0))
        st, js, _ = self.req("POST", "/api/node", {"name": "Zahrada"}, headers=h)
        self.assertFalse(js["recalc"])                           # a name does not touch the levels
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
        st, js, _ = self.req("POST", "/api/pairing/start", {"node_id": "5555", "name": "  "}, headers=h)
        self.assertEqual(st, 400)                                # a new sensor needs a name

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
        self.assertIsNone(n2.try_pair())                         # confirmation (key kept a moment)
        time.sleep(0.3)
        self.assertIn(0x9999, self.core._unpair_confirm)
        self.core._unpair_confirm[0x9999] -= 60                  # sensor stayed unpaired
        self.assertIsNone(n2.try_pair())                         # -> removed
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

        # Home Assistant only over MQTT (the REST API and its port are gone)
        st, js, _ = self.req("GET", "/api/mqtt", headers=h)
        self.assertEqual((st, js["configured"]), (200, False))

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

    def test_1b_update_api(self):
        """Check / install requests for the root updater, prefs, status; admin + CSRF only."""
        from retencka import __version__
        d = self.tmp.name
        req_file, status_file = os.path.join(d, "update-request"), os.path.join(d, "update-status.json")
        st, _, _ = self.req("GET", "/api/update/status")
        self.assertEqual(st, 401)
        h = self.login()
        st, js, _ = self.req("GET", "/api/update/status", headers=h)
        self.assertEqual(st, 200)
        self.assertEqual(js["running"], __version__)
        self.assertFalse(js["pending"])
        self.assertIn(js["prefs"]["channel"], ("stable", "beta"))
        # CSRF required
        st, _, _ = self.req("POST", "/api/update/check", {}, headers={"Cookie": h["Cookie"]})
        self.assertEqual(st, 403)
        st, js, _ = self.req("POST", "/api/update/check", {}, headers=h)
        self.assertEqual(st, 200, js)
        self.assertTrue(js["pending"])
        with open(req_file) as f:
            self.assertEqual(json.load(f)["action"], "check")
        # the old route still asks for a check (never an install)
        st, js, _ = self.req("POST", "/api/system/update", {}, headers=h)
        self.assertEqual(st, 200)
        with open(req_file) as f:
            self.assertEqual(json.load(f)["action"], "check")
        # install only of the version the last check found
        st, js, _ = self.req("POST", "/api/update/install", {"version": "9.9.9"}, headers=h)
        self.assertEqual(st, 409)
        with open(status_file, "w") as f:
            json.dump({"state": "available", "current": __version__, "latest": "9.9.9",
                       "updated_at": int(time.time())}, f)
        for bad in ("9.9.9; rm", 9, None, "../9"):
            st, _, _ = self.req("POST", "/api/update/install", {"version": bad}, headers=h)
            self.assertEqual(st, 400, bad)
        st, js, _ = self.req("POST", "/api/update/install", {"version": "9.9.8"}, headers=h)
        self.assertEqual(st, 409)
        st, js, _ = self.req("POST", "/api/update/install", {"version": "9.9.9"}, headers=h)
        self.assertEqual(st, 200, js)
        with open(req_file) as f:
            body = json.load(f)
        body.pop("ts")
        self.assertEqual(body, {"action": "install", "version": "9.9.9"})
        # while installing, no new request is accepted
        with open(status_file, "w") as f:
            json.dump({"state": "installing", "latest": "9.9.9", "updated_at": int(time.time())}, f)
        st, _, _ = self.req("POST", "/api/update/check", {}, headers=h)
        self.assertEqual(st, 409)
        st, js, _ = self.req("GET", "/api/update/status", headers=h)
        self.assertTrue(js["busy"])
        self.assertEqual(js["status"]["state"], "installing")
        # prefs: strict validation
        for bad in ({"auto": "yes", "channel": "stable"}, {"auto": True, "channel": "nightly"}, {}):
            st, _, _ = self.req("POST", "/api/update/prefs", bad, headers=h)
            self.assertEqual(st, 400, bad)
        st, js, _ = self.req("POST", "/api/update/prefs", {"auto": False, "channel": "beta"}, headers=h)
        self.assertEqual(st, 200, js)
        with open(os.path.join(d, "update-prefs.json")) as f:
            self.assertEqual(json.load(f), {"auto": False, "channel": "beta"})
        st, js, _ = self.req("GET", "/api/system", headers=h)
        self.assertEqual(js["update_prefs"], {"auto": False, "channel": "beta"})
        self.assertIn("cpu_pct", js)
        self.assertIn("ram_used_mb", js)
        self.assertIn("ram_total_mb", js)
        self.assertIn("ram_pct", js)
        if js["cpu_pct"] is not None:
            self.assertGreaterEqual(js["cpu_pct"], 0.0)
            self.assertLessEqual(js["cpu_pct"], 100.0)
        if js["ram_pct"] is not None:
            self.assertGreaterEqual(js["ram_pct"], 0.0)
            self.assertLessEqual(js["ram_pct"], 100.0)
        for p in (req_file, status_file, os.path.join(d, "update-prefs.json")):
            os.remove(p)

    def test_1d_netbird_install_request(self):
        h = self.login()
        key = "ABCDEF01-2345-6789-ABCD-EF0123456789"
        for bad in ({"setup_key": "short"}, {"setup_key": "x; rm -rf / #aaaaaaaaaaaaaaa"},
                    {"setup_key": key, "management_url": "http://plain.cz"},
                    {"setup_key": key, "management_url": "https://a.cz/$(id)"}):
            st, _, _ = self.req("POST", "/api/servis/install", bad, headers=h)
            self.assertEqual(st, 400, bad)
        st, js, _ = self.req("POST", "/api/servis/install", {"setup_key": key, "management_url": "https://nb.example.cz"},
                             headers=h)
        self.assertEqual(st, 200, js)
        req = os.path.join(self.tmp.name, "control-netbird-install")
        with open(req) as f:
            self.assertEqual(json.load(f), {"setup_key": key, "management_url": "https://nb.example.cz"})
        st, js, _ = self.req("GET", "/api/servis", headers=h)
        self.assertEqual(js["install"]["state"], "queued")
        self.assertNotIn(key, json.dumps(js))                              # never echoed back
        st, _, _ = self.req("POST", "/api/servis/install", {"setup_key": key}, headers=h)
        self.assertEqual(st, 400)                                          # already running
        details = " ".join(r["detail"] or "" for r in self.core.db.query("SELECT detail FROM audit"))
        self.assertNotIn(key, details)
        os.remove(req)
        os.remove(os.path.join(self.tmp.name, "netbird-install.json"))

    def test_1e_geocode_and_hydrawise(self):
        """Place search and Hydrawise zones/test with the network mocked; key never returned."""
        h = self.login()
        calls = []
        replies = {}

        async def fake_get(url, params):
            calls.append((url, dict(params)))
            for part, reply in replies.items():
                if part in url:
                    return reply
            return 500, "{}"
        orig = self.core.http.get
        self.core.http.get = fake_get
        try:
            replies["geocoding-api.open-meteo.com"] = (200, json.dumps({"results": [
                {"name": "Praha", "admin1": "Hlavní město Praha", "country": "Česko", "latitude": 50.08804,
                 "longitude": 14.42076, "population": 1165581},
                {"name": "Broken", "latitude": "x", "longitude": 1}]}))
            st, js, _ = self.req("GET", "/api/geocode?q=Praha", headers=h)
            self.assertEqual(st, 200, js)
            self.assertEqual(js["results"], [{"name": "Praha", "admin1": "Hlavní město Praha", "country": "Česko",
                                              "lat": 50.08804, "lon": 14.42076}])
            self.assertEqual(calls[-1][1]["name"], "Praha")
            self.assertEqual(calls[-1][1]["language"], "cs")
            for q in ("x", "a" * 81, "%01ab"):
                st, _, _ = self.req("GET", "/api/geocode?q=" + q, headers=h)
                self.assertEqual(st, 400, q)
            st, _, _ = self.req("GET", "/api/geocode?q=Praha", headers={"Cookie": "x"})
            self.assertEqual(st, 401)

            nid = self.core.nodes.ids()[0]
            node = f"{nid:04X}"
            self.req("POST", "/api/settings", {"hydrawise_api_key": "SECRETKEY123456"}, headers=h)
            replies["statusschedule.php"] = (200, json.dumps({"relays": [
                {"relay_id": 2222, "relay": 2, "name": "Zahrada"}, {"relay_id": 1111, "relay": 1, "name": "Trávník"},
                {"relay_id": "evil<script>", "relay": 3, "name": "x"}]}))
            st, js, _ = self.req("GET", f"/api/hydrawise/zones?node={node}", headers=h)
            self.assertEqual(st, 200, js)
            self.assertEqual(js["zones"], [{"relay_id": "1111", "name": "Trávník", "number": 1},
                                           {"relay_id": "2222", "name": "Zahrada", "number": 2}])
            # the tank's own key wins over the account key
            self.assertEqual(calls[-1][1]["api_key"], self.core.nodes.get(nid)["hydrawise_api_key"] or "SECRETKEY123456")
            replies["customerdetails.php"] = (200, json.dumps({"controllers": [{"name": "HC u domu", "controller_id": 9}]}))
            st, js, _ = self.req("POST", "/api/hydrawise/test", {}, headers=h)
            self.assertEqual(st, 200, js)
            self.assertEqual(js["controllers"], ["HC u domu"])
            self.assertNotIn("SECRETKEY123456", json.dumps(js))
            replies["customerdetails.php"] = (200, json.dumps({"error_msg": "unauthorised"}))
            st, js, _ = self.req("POST", "/api/hydrawise/test", {"api_key": "OTHERKEY99999"}, headers=h)
            self.assertEqual(st, 502)
            self.assertEqual(calls[-1][1]["api_key"], "OTHERKEY99999")
            # the pasted key in the zone field is refused with a clear message
            st, js, _ = self.req("POST", f"/api/node?node={node}", {"hydrawise_relay_id": "SECRETKEY123456"}, headers=h)
            self.assertEqual(st, 400)
            self.assertIn("API klíč", js["error"])
            st, js, _ = self.req("GET", "/api/settings", headers=h)
            self.assertNotIn("SECRETKEY123456", json.dumps(js))
        finally:
            self.core.http.get = orig
            self.req("POST", "/api/settings", {"hydrawise_api_key": ""}, headers=h)

    def test_1f_power_and_data_delete(self):
        from unittest import mock
        h = self.login()
        d = self.tmp.name
        # CSRF / login required
        for path in ("/api/system/restart", "/api/system/reboot", "/api/system/poweroff", "/api/data/delete"):
            st, _, _ = self.req("POST", path, {}, headers={"Cookie": h["Cookie"]})
            self.assertEqual(st, 403, path)
        with mock.patch("os.kill") as kill:
            st, js, _ = self.req("POST", "/api/system/restart", {}, headers=h)
            self.assertEqual(st, 200, js)
            time.sleep(1.5)                                    # restart is scheduled after the response
            self.assertTrue(kill.called)
        st, js, _ = self.req("POST", "/api/system/reboot", {}, headers=h)
        self.assertEqual(st, 200)
        self.assertTrue(os.path.exists(os.path.join(d, "control-reboot")))
        st, js, _ = self.req("POST", "/api/system/poweroff", {}, headers=h)
        self.assertEqual(st, 200)
        self.assertTrue(os.path.exists(os.path.join(d, "control-poweroff")))
        actions = [r["action"] for r in self.core.db.query("SELECT action FROM audit ORDER BY id DESC LIMIT 3")]
        self.assertEqual(actions, ["SERVICE_POWEROFF", "SERVICE_REBOOT", "SERVICE_RESTART"])
        for f in ("control-reboot", "control-poweroff"):
            os.remove(os.path.join(d, f))
        # deleting data: dry run first, validation, audit entry
        node = f"{self.core.nodes.ids()[0]:04X}"
        st, js, _ = self.req("POST", "/api/data/delete", {"node": node, "dry_run": True}, headers=h)
        self.assertEqual(st, 200, js)
        before = js["counts"]["measurements"]
        self.assertGreater(before, 0)
        self.assertEqual(self.core.db.scalar("SELECT COUNT(*) FROM measurements WHERE node_id=?",
                                             (int(node, 16),)), before)
        for bad in ({"node": node, "from": 100, "to": 50, "dry_run": False}, {"from": "x", "dry_run": False},
                    {"node": "FFFE", "dry_run": False}):
            st, _, _ = self.req("POST", "/api/data/delete", bad, headers=h)
            self.assertIn(st, (400, 404), bad)
        now = int(time.time())
        st, js, _ = self.req("POST", "/api/data/delete", {"node": node, "from": now + 86400, "to": now + 2 * 86400,
                                                          "dry_run": False}, headers=h)
        self.assertEqual((st, js["total"]), (200, 0))                       # empty range: nothing
        row = self.core.db.one("SELECT detail FROM audit WHERE action='DATA_DELETED' ORDER BY id DESC")
        self.assertIn(node, row["detail"])

    def test_1c_node_defaults_api(self):
        h = self.login()
        st, js, _ = self.req("GET", "/api/node-defaults", headers=h)
        self.assertEqual(st, 200)
        self.assertIn("notify_low_pct", js["keys"])
        nid = self.core.nodes.ids()[0]
        node = f"{nid:04X}"
        st, js, _ = self.req("GET", "/api/nodes", headers=h)
        mine = next(n for n in js["nodes"] if n["id"] == node)
        self.assertIn("inherited", mine)
        # own value for this tank, then a default change must not touch it
        st, js, _ = self.req("POST", f"/api/node?node={node}", {"notify_low_pct": 44}, headers=h)
        self.assertEqual(st, 200, js)
        self.assertNotIn("notify_low_pct", js["node"]["inherited"])
        st, js, _ = self.req("POST", "/api/node-defaults", {"notify_low_pct": 11, "rain_wait_hours": 12}, headers=h)
        self.assertEqual(st, 200, js)
        self.assertEqual(sorted(js["changed"]), ["notify_low_pct", "rain_wait_hours"])
        mine = next(n for n in js["nodes"] if n["id"] == node)
        self.assertEqual((mine["notify_low_pct"], mine["rain_wait_hours"]), (44, 12))
        st, js, _ = self.req("POST", "/api/node-defaults", {"tank_area_m2": 3}, headers=h)
        self.assertEqual(st, 400)
        st, js, _ = self.req("POST", "/api/node-defaults", {"notify_low_pct": 11}, headers={"Cookie": h["Cookie"]})
        self.assertEqual(st, 403)                                         # CSRF
        # reset → inherited again
        st, js, _ = self.req("POST", f"/api/node?node={node}", {"reset": ["notify_low_pct"]}, headers=h)
        self.assertEqual(st, 200, js)
        self.assertEqual(js["node"]["notify_low_pct"], 11)
        self.assertIn("notify_low_pct", js["node"]["inherited"])
        st, js, _ = self.req("POST", f"/api/node?node={node}", {"reset": "notify_low_pct"}, headers=h)
        self.assertEqual(st, 400)
        self.req("POST", "/api/node-defaults", {"notify_low_pct": 15, "rain_wait_hours": 24}, headers=h)

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

    def raw(self, data: bytes, port=None) -> bytes:
        s = socket.create_connection(("127.0.0.1", port or self.web_port), timeout=10)
        try:
            s.sendall(data)
            out = b""
            while b"\r\n\r\n" not in out:
                chunk = s.recv(4096)
                if not chunk:
                    break
                out += chunk
            return out
        finally:
            s.close()

    def test_3_lockout_is_generic_and_does_not_block_owner(self):
        self.core.ip_limiter.success("127.0.0.1")
        msgs = set()
        for _ in range(6):                      # > USER_MAX_FAILS wrong passwords for the real account
            st, js, _ = self.req("POST", "/api/auth/login", {"username": "admin2", "password": "wrong-password-x"})
            self.assertEqual(st, 401)
            msgs.add(js["error"])
            self.core.ip_limiter.success("127.0.0.1")
        st, js, _ = self.req("POST", "/api/auth/login", {"username": "nobody", "password": "wrong-password-x"})
        self.assertEqual(st, 401)
        msgs.add(js["error"])
        self.assertEqual(len(msgs), 1)          # no oracle: unknown / locked / wrong all look the same
        locks = self.web_app["login_locks"]
        # the pair (admin2, 127.0.0.1) is locked, but the same account from another address is not
        self.assertTrue(locks.locked("admin2", "127.0.0.1"))
        self.assertFalse(locks.locked("admin2", "203.0.113.9"))
        st, js, _ = self.req("POST", "/api/auth/login", {"username": "admin2", "password": "Velmi-Tajne-Heslo1"})
        self.assertEqual((st, js["error"]), (401, next(iter(msgs))))     # locked looks like a wrong password
        locks.success("admin2", "127.0.0.1")
        self.core.ip_limiter.success("127.0.0.1")
        st, _, _ = self.req("POST", "/api/auth/login", {"username": "admin2", "password": "Velmi-Tajne-Heslo1"})
        self.assertEqual(st, 200)

    def test_3b_theme_per_user(self):
        h = self.login()
        st, me, _ = self.req("GET", "/api/me", headers=h)
        self.assertEqual(me["theme"], "auto")
        st, js, _ = self.req("POST", "/api/me/theme", {"theme": "dark"}, headers=h)
        self.assertEqual(st, 200, js)
        st, me, _ = self.req("GET", "/api/me", headers=self.login())     # new session, same user
        self.assertEqual(me["theme"], "dark")
        st, js, _ = self.req("POST", "/api/me/theme", {"theme": "neon"}, headers=h)
        self.assertEqual(st, 400)
        st, js, _ = self.req("POST", "/api/me/theme", {"theme": "dark"},
                             headers={"Cookie": h["Cookie"]})           # without CSRF token
        self.assertEqual(st, 403)
        self.req("POST", "/api/me/theme", {"theme": "auto"}, headers=h)

    def test_4_http_hardening(self):
        for cl in ("+5", "1_0", "5 5", "-1", "0x5", "٥"):
            data = ("POST /api/auth/login HTTP/1.1\r\nHost: x\r\nContent-Length: %s\r\n\r\n" % cl).encode("utf-8")
            self.assertTrue(self.raw(data).startswith(b"HTTP/1.1 400"), cl)
        many = "&".join(f"a{i}=1" for i in range(80))
        out = self.raw(f"GET /api/auth/mode?{many} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        self.assertTrue(out.startswith(b"HTTP/1.1 400"), out[:40])
        # multiple Cookie headers are merged (RFC 6265 §5.4)
        h = self.login()
        out = self.raw(f"GET /api/me HTTP/1.1\r\nHost: x\r\nCookie: other=1\r\nCookie: {h['Cookie']}\r\n\r\n".encode())
        self.assertTrue(out.startswith(b"HTTP/1.1 200"), out[:40])

    def test_5_non_ascii_header(self):
        out = self.raw("GET /api/me HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer héslo\r\n\r\n".encode("latin-1"))
        self.assertTrue(out.startswith(b"HTTP/1.1 401"), out[:40])

    def test_6_reserved_usernames_and_huge_numbers(self):
        from retencka import auth
        self.assertIsNotNone(auth.username_problem("System"))
        self.assertIsNotNone(auth.username_problem("system-bot"))
        self.assertIsNone(auth.username_problem("pepa"))
        h = self.login()
        st, _, _ = self.req("GET", "/api/history?from=1e999", headers=h)
        self.assertEqual(st, 400)
        st, js, _ = self.req("GET", "/api/audit?page=" + "9" * 400, headers=h)
        self.assertEqual(st, 200)

    def test_7_stream_error_closes_connection(self):
        from retencka import httpserver as hs
        app = hs.Application()

        async def boom(request):
            resp = hs.StreamResponse()
            await resp.prepare(request)
            await resp.write(b"abc")
            raise RuntimeError("late failure")
        app.router.add_get("/boom", boom)
        port = free_port()
        asyncio.run_coroutine_threadsafe(hs.Server(app).start("127.0.0.1", port), self.loop).result(10)
        s = socket.create_connection(("127.0.0.1", port), timeout=10)
        s.sendall(b"GET /boom HTTP/1.1\r\nHost: x\r\n\r\n")
        data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        s.close()
        self.assertNotIn(b"500", data)
        self.assertEqual(data.count(b"HTTP/1.1"), 1)

    def test_8_tunnel_secure_cookie_and_origin_host_check(self):
        st, js, r = self.req("POST", "/api/auth/login",
                             {"username": "admin2", "password": "Velmi-Tajne-Heslo1"},
                             headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(st, 200, js)
        cookie = r.getheader("Set-Cookie")
        self.assertIsNotNone(cookie)
        self.assertIn("Secure", cookie)

        h = self.login()
        h.update({
            "Origin": "https://tunnel.example",
            "X-Forwarded-Host": "tunnel.example",
        })
        st, js, _ = self.req("POST", "/api/node", {"name": "Test"}, headers=h)
        self.assertEqual(st, 403)
        self.assertIn("původ", (js or {}).get("error", "").lower())

    def test_9_i18n_dictionary_covers_static_html(self):
        import re
        from html.parser import HTMLParser
        from pathlib import Path

        static_dir = Path(__file__).resolve().parent.parent / "static"
        html_path = static_dir / "index.html"
        i18n_path = static_dir / "js" / "i18n.js"

        class TextExtractor(HTMLParser):
            def __init__(self):
                super().__init__()
                self.texts = []
                self._ignored = 0

            def handle_starttag(self, tag, attrs):
                if tag in ("script", "style"):
                    self._ignored += 1

            def handle_endtag(self, tag):
                if tag in ("script", "style") and self._ignored > 0:
                    self._ignored -= 1

            def handle_data(self, data):
                if self._ignored == 0:
                    txt = data.strip()
                    if txt and re.search(r"[áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]", txt):
                        self.texts.append(txt)

        extractor = TextExtractor()
        extractor.feed(html_path.read_text(encoding="utf-8"))

        js_content = i18n_path.read_text(encoding="utf-8")
        dict_match = re.search(r"const DICT = \{(.*?)\n\s*\};", js_content, re.DOTALL)
        self.assertIsNotNone(dict_match, "const DICT not found in i18n.js")
        dict_keys = set(re.findall(r'^\s*"([^"]+)"\s*:', dict_match.group(1), re.M))

        self.assertGreater(len(extractor.texts), 0)
        found = [t for t in extractor.texts if t in dict_keys]
        coverage_pct = len(found) / len(extractor.texts) * 100.0
        self.assertGreaterEqual(coverage_pct, 95.0,
                                f"i18n DICT covers only {coverage_pct:.1f}% (< 95%) of static HTML phrases")


if __name__ == "__main__":
    unittest.main()

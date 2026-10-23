"""Telegram bot (linking, roles, commands, notifications), PNG chart, state watch, service access."""
import asyncio
import json
import os
import struct
import sys
import tempfile
import time
import unittest
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from retencka import model, pngchart  # noqa: E402
from retencka.core import Core  # noqa: E402
from retencka.db import Database  # noqa: E402
from retencka.settings import Settings  # noqa: E402
from retencka.telegram import TelegramBot  # noqa: E402

NID = 0x5555


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Database(os.path.join(self.tmp.name, "t.db"))
        self.core = Core(self.db, Settings(self.db), self.tmp.name)
        now = int(time.time())
        for name, role in (("admin", "admin"), ("pepa", "user")):
            self.db.execute("INSERT INTO users(username, pw_hash, role, created, pw_changed) VALUES(?,?,?,?,?)",
                            (name, "x", role, now, now))
        self.core.nodes.add(NID, "Zahrada", {"tank_area_m2": 10.0, "max_depth_cm": 100.0, "notify_low_pct": 20.0})
        self.core.last[NID] = {"ts": now, "level_mm": 500, "flags": 0, "vbat_mv": 4900}

    def tearDown(self):
        self.tmp.cleanup()


class TelegramTest(Base):
    def setUp(self):
        super().setUp()
        self.bot = TelegramBot(self.core)
        self.bot.token, self.bot.username = "123:abc", "nadrz_bot"
        self.sent = []

        async def fake_api(method, payload=None, files=None):
            self.sent.append((method, payload, files))
            return {"ok": True}
        self.bot.api = fake_api

    def msg(self, chat_id, text, kind="private"):
        upd = {"message": {"chat": {"id": chat_id, "type": kind, "first_name": "Test"}, "text": text}}

        async def run():
            self.bot.loop = asyncio.get_running_loop()
            await self.bot.handle(upd)
            await asyncio.sleep(0)
        asyncio.run(run())
        return [p["text"] for m, p, _ in self.sent if m == "sendMessage"]

    def link(self, chat_id, user):
        code = self.bot.new_code(user)["code"]
        self.msg(chat_id, f"/start {code}")
        self.sent.clear()

    def test_link_and_roles(self):
        self.assertIn("není propojený", self.msg(1, "stav")[-1])
        self.link(1, "pepa")
        self.assertEqual(self.bot.chats("pepa")[0]["chat_id"], 1)
        self.assertIn("Zahrada", self.msg(1, "/stav")[-1])
        self.assertIn("oprávnění", self.msg(1, "dopustit 60")[-1])          # user: read only
        self.link(2, "admin")
        self.assertIn("Dopouštění není pro tuto nádrž nastaveno", self.msg(2, "dopustit 60")[-1])
        self.assertIn("Hub", self.msg(2, "diag")[-1])

    def test_update_check_then_install(self):
        req = os.path.join(self.tmp.name, "update-request")
        self.link(2, "admin")
        self.assertIn("Hledám", self.msg(2, "aktualizovat")[-1])
        with open(req) as f:
            self.assertEqual(json.load(f)["action"], "check")           # a check never installs
        # install only of the version the last check reported
        self.assertIn("není k dispozici", self.msg(2, "aktualizovat instalovat 9.9.9")[-1])
        with open(os.path.join(self.tmp.name, "update-status.json"), "w") as f:
            json.dump({"state": "available", "latest": "9.9.9", "updated_at": int(time.time())}, f)
        self.assertIn("Instaluji verzi 9.9.9", self.msg(2, "aktualizovat instalovat 9.9.9")[-1])
        with open(req) as f:
            self.assertEqual(json.load(f)["version"], "9.9.9")
        self.link(1, "pepa")
        self.assertIn("oprávnění", self.msg(1, "aktualizovat")[-1])

    def test_refill_target_validated(self):
        self.link(2, "admin")
        for bad in ("nan", "inf", "-5", "0", "101", "1e999"):
            self.sent.clear()
            self.assertIn("1–100", self.msg(2, f"dopustit {bad}")[-1], bad)

    def test_call_survives_http_exceptions(self):
        import http.client
        from unittest import mock
        from retencka import telegram
        with mock.patch("urllib.request.urlopen", side_effect=http.client.IncompleteRead(b"")):
            res = telegram._call("1:a", "getMe")
        self.assertFalse(res["ok"])

    def test_poll_thread_survives_crash(self):
        from unittest import mock
        calls = []

        def loop():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            self.bot._stop.set()
        with mock.patch.object(self.bot, "_poll_loop", side_effect=loop),                 mock.patch.object(self.bot._stop, "wait", return_value=False):
            self.bot._poll_thread()
        self.assertEqual(len(calls), 2)

    def test_code_single_use_and_rate_limit(self):
        code = self.bot.new_code("pepa")["code"]
        self.msg(1, f"/start {code}")
        self.assertIn("neplatný", self.msg(3, f"/start {code}")[-1])         # already used
        for _ in range(5):
            self.msg(4, "/start WRONG000")
        self.assertIn("Příliš mnoho", self.msg(4, "/start WRONG000")[-1])

    def test_groups_refused(self):
        self.assertIn("soukromém", self.msg(-100, "stav", kind="group")[-1])

    def test_notify_respects_role_and_switch(self):
        self.link(1, "pepa")
        self.link(2, "admin")
        self.msg(1, "upozorneni vyp")
        self.sent.clear()

        async def run():
            self.bot.loop = asyncio.get_running_loop()
            self.bot.notify("vše", False)
            self.bot.notify("admin", True)
            await asyncio.sleep(0.05)
        asyncio.run(run())
        got = sorted((p["chat_id"], p["text"]) for m, p, _ in self.sent if m == "sendMessage")
        self.assertEqual(got, [(2, "admin"), (2, "vše")])

    def test_deleting_user_removes_chat(self):
        self.link(1, "pepa")
        self.db.execute("DELETE FROM users WHERE username='pepa'")
        self.assertEqual(self.bot.chats(), [])


class WatchTest(Base):
    def test_low_level_and_offline_once(self):
        got = []
        self.core.notifier = lambda text, admin: got.append(text)
        self.core.watch_tick()                                  # first pass: only records
        self.core.last[NID]["level_mm"] = 150                   # 15 % < 20 %
        self.core.watch_tick()
        self.core.watch_tick()
        self.assertEqual(len([t for t in got if "hladina" in t]), 1)
        self.core.last[NID]["level_mm"] = 210                   # 21 %: within hysteresis
        self.core.watch_tick()
        self.core.last[NID]["ts"] -= 7200                       # silent sensor
        self.core.watch_tick()
        self.assertTrue(any("neposílá data" in t for t in got))

    def test_audit_forwarded(self):
        got = []
        self.core.notifier = lambda text, admin: got.append((text, admin))
        self.core.audit("REFILL_FAULT", "Zahrada: hladina nestoupla")
        self.core.audit("LOGIN_SUCCESS", "nic")
        self.assertEqual(len(got), 1)
        self.assertIn("nestoupla", got[0][0])


class ServisTest(Base):
    def test_modes_and_expiry(self):
        st = self.core.set_servis("24h", "admin")
        self.assertEqual(st["mode"], "24h")
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "control-servis")))
        path = os.path.join(self.tmp.name, "servis.json")
        with open(path) as f:
            d = json.load(f)
        d["until"] = int(time.time()) - 1
        with open(path, "w") as f:
            json.dump(d, f)
        self.assertEqual(self.core.servis_state()["mode"], "off")      # expired
        with self.assertRaises(ValueError):
            self.core.set_servis("forever", "admin")


class ChartTest(unittest.TestCase):
    def test_png(self):
        now = int(time.time())
        t = [(now - 7 * 86400 + i * 3600) * 1000 for i in range(168)]
        pct = [50 + 20 * ((i % 24) / 24) if i % 50 else None for i in range(168)]
        png = pngchart.level_chart(t, pct, now - 7 * 86400, now,
                                   [{"kind": "refill", "start": t[10], "end": t[12]}], w=320, h=160)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        w, h = struct.unpack(">II", png[16:24])
        self.assertEqual((w, h), (320, 160))
        idat = png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8]
        self.assertEqual(len(zlib.decompress(idat)), h * (1 + w * 3))


if __name__ == "__main__":
    unittest.main()

"""Home Assistant over MQTT: packets, discovery, states, commands, removal – against a fake broker."""
import asyncio
import json
import os
import struct
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from retencka import mqtt  # noqa: E402
from retencka.core import Core  # noqa: E402
from retencka.db import Database  # noqa: E402
from retencka.settings import Settings, SettingsError  # noqa: E402

NID = 0x5555


class FakeBroker:
    """Just enough MQTT 3.1.1 broker: login, retained messages, subscriptions (exact / '+')."""

    def __init__(self, user="retencka", password="tajne"):
        self.user, self.password = user, password
        self.retained: dict[str, bytes] = {}
        self.log: list[tuple[str, bytes]] = []
        self.connects: list[dict] = []
        self.clients: list[tuple[asyncio.StreamWriter, list[str]]] = []
        self.server = None
        self.port = 0

    async def start(self):
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def close(self):
        for w, _ in self.clients:
            w.close()
        self.server.close()
        await self.server.wait_closed()

    @staticmethod
    def _s(b: bytes, i: int) -> tuple[bytes, int]:
        n = struct.unpack(">H", b[i:i + 2])[0]
        return b[i + 2:i + 2 + n], i + 2 + n

    async def _client(self, reader, writer):
        subs: list[str] = []
        try:
            while True:
                first, body = await mqtt.read_packet(reader)
                kind = first >> 4
                if kind == 1:                                   # CONNECT
                    proto, i = self._s(body, 0)
                    flags = body[i + 1]
                    i += 4
                    cid, i = self._s(body, i)
                    info = {"proto": proto, "flags": flags, "client": cid.decode()}
                    if flags & 0x04:
                        info["will_topic"], i = self._s(body, i)
                        info["will"], i = self._s(body, i)
                    user = pw = b""
                    if flags & 0x80:
                        user, i = self._s(body, i)
                    if flags & 0x40:
                        pw, i = self._s(body, i)
                    self.connects.append(info)
                    ok = user.decode() == self.user and pw.decode() == self.password
                    writer.write(bytes([0x20, 2, 0, 0 if ok else 4]))
                    await writer.drain()
                    if not ok:
                        return
                    self.clients.append((writer, subs))
                elif kind == 3:                                 # PUBLISH
                    topic, payload, _, _ = mqtt.parse_publish(first, body)
                    self.log.append((topic, payload))
                    if first & 1:
                        if payload:
                            self.retained[topic] = payload
                        else:
                            self.retained.pop(topic, None)
                elif kind == 8:                                 # SUBSCRIBE
                    i = 2
                    while i < len(body):
                        t, i = self._s(body, i)
                        subs.append(t.decode())
                        i += 1
                    writer.write(bytes([0x90, 3]) + body[:2] + b"\x00")
                    await writer.drain()
                elif kind == 12:                                # PINGREQ
                    writer.write(b"\xd0\x00")
                    await writer.drain()
                elif kind == 14:                                # DISCONNECT
                    return
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    @staticmethod
    def _match(flt: str, topic: str) -> bool:
        f, t = flt.split("/"), topic.split("/")
        return len(f) == len(t) and all(a in ("+", b) for a, b in zip(f, t))

    async def send(self, topic: str, payload: bytes):
        for w, subs in self.clients:
            if any(self._match(s, topic) for s in subs):
                w.write(mqtt.publish_packet(topic, payload))
                await w.drain()


async def until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return False


class PacketTest(unittest.TestCase):
    def test_connect_packet(self):
        p = mqtt.connect_packet("retencka-x", "u", "p", "t/status", b"offline")
        self.assertEqual(p[0], 0x10)
        self.assertEqual(p[2:8], b"\x00\x04MQTT")
        self.assertEqual(p[8], 4)
        self.assertEqual(p[9], 0x02 | 0x04 | 0x20 | 0x80 | 0x40)
        # a password without a user name is not sent (MQTT 3.1.1)
        self.assertEqual(mqtt.connect_packet("c", "", "p", "t", b"x")[9] & 0xC0, 0)

    def test_publish_roundtrip(self):
        p = mqtt.publish_packet("a/b", b"x" * 300, retain=True)
        self.assertEqual(p[0], 0x31)
        self.assertEqual(p[1:3], b"\xb1\x02")            # 2 + 3 + 300 = 305 = varint b1 02

        async def run():
            r = asyncio.StreamReader()
            r.feed_data(p)
            r.feed_eof()
            first, body = await mqtt.read_packet(r)
            return mqtt.parse_publish(first, body)
        topic, payload, qos, pid = asyncio.run(run())
        self.assertEqual((topic, payload, qos, pid), ("a/b", b"x" * 300, 0, None))

    def test_host_check(self):
        s = Settings(Database(":memory:"))
        s.update({"mqtt_host": "mqtt://homeassistant.local/"})
        self.assertEqual(s.get("mqtt_host"), "homeassistant.local")
        for bad in ("homeassistant.local:1883", "a b", "x;rm"):
            with self.assertRaises(SettingsError, msg=bad):
                s.update({"mqtt_host": bad})
        s.update({"mqtt_password": "tajne-heslo"})
        self.assertEqual(s.public()["mqtt_password"], "•" * 8)          # nothing of it shown
        self.assertEqual(len(s.get("mqtt_uid")), 8)


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Database(os.path.join(self.tmp.name, "t.db"))
        self.core = Core(self.db, Settings(self.db), self.tmp.name)
        self.core.nodes.add(NID, "Zahrada", {"tank_area_m2": 10.0, "max_depth_cm": 100.0})
        self.core.last[NID] = {"ts": int(time.time()), "level_mm": 500, "flags": 0, "vbat_mv": 4900,
                               "temp_x100": 1234, "hum_x100": 5600, "rssi_x2": -90, "fw": 10}
        self.calls = []

        async def fake_start(nid, target, mode, user, ip=""):
            self.calls.append(("start", nid, target, user))
            return True, "ok"

        async def fake_cancel(nid, reason, user, ip=""):
            self.calls.append(("stop", nid, user))
            return True, "ok"
        self.core.refill_start, self.core.refill_cancel = fake_start, fake_cancel

    def tearDown(self):
        self.tmp.cleanup()

    def run_with_broker(self, body, **kw):
        async def run():
            broker = FakeBroker(**kw)
            await broker.start()
            self.core.loop = asyncio.get_running_loop()
            self.core.settings.update({"mqtt_host": "127.0.0.1", "mqtt_port": broker.port,
                                       "mqtt_username": "retencka", "mqtt_password": "tajne"})
            bridge = mqtt.MqttBridge(self.core)
            self.core.mqtt = bridge
            bridge.start(self.core.loop)
            try:
                await body(broker, bridge)
            finally:
                await bridge.stop()
                await broker.close()
        asyncio.run(run())

    def test_discovery_state_and_commands(self):
        uid = self.core.settings.get("mqtt_uid")
        base, obj = f"retencka/{uid}", f"retencka_{uid}_5555"
        cfg_t = f"homeassistant/sensor/{obj}/hladina/config"

        async def body(b, bridge):
            self.assertTrue(await until(lambda: f"{base}/5555/state" in b.retained))
            self.assertTrue(bridge.status()["connected"])
            self.assertEqual(b.connects[0]["will"], b"offline")
            self.assertEqual(b.retained[f"{base}/status"], b"online")
            cfg = json.loads(b.retained[cfg_t])
            self.assertEqual(cfg["device"]["name"], "Zahrada")
            self.assertEqual(cfg["state_topic"], f"{base}/5555/state")
            st = json.loads(b.retained[f"{base}/5555/state"])
            self.assertEqual((st["level_pct"], st["temperature"], st["humidity"], st["rssi"]), (50.0, 12.34, 56.0, -45.0))
            self.assertEqual(b.retained[f"{base}/5555/availability"], b"online")
            # discovery is published before the first state
            topics = [t for t, _ in b.log]
            self.assertLess(topics.index(cfg_t), topics.index(f"{base}/5555/state"))
            # no valve: no refill buttons, a command does nothing
            self.assertFalse(any("/button/" in t for t in b.retained))
            # with a valve: buttons appear, commands reach the refill controller
            self.core._provider = lambda node, for_stop=False: object()
            bridge.kick()
            self.assertTrue(await until(lambda: f"homeassistant/button/{obj}/dopustit/config" in b.retained))
            await b.send(f"{base}/5555/refill/set", b"START")
            await b.send(f"{base}/5555/refill/set", b"STOP")
            await b.send(f"{base}/ffff/refill/set", b"START")          # unknown tank
            self.assertTrue(await until(lambda: len(self.calls) == 2))
            self.assertEqual(self.calls[0], ("start", NID, 30.0, "Home Assistant"))
            self.assertEqual(self.calls[1], ("stop", NID, "Home Assistant"))
            # control switched off: buttons removed, commands ignored
            self.core.settings.update({"mqtt_control": False})
            bridge.reconfigure()
            self.assertTrue(await until(lambda: f"homeassistant/button/{obj}/dopustit/config" not in b.retained))
            await b.send(f"{base}/5555/refill/set", b"START")
            await asyncio.sleep(0.2)
            self.assertEqual(len(self.calls), 2)
            # Home Assistant restarted: everything is sent again
            n = len(b.log)
            await b.send("homeassistant/status", b"online")
            self.assertTrue(await until(lambda: sum(1 for t, _ in b.log[n:] if t == cfg_t) == 1))
            # renamed tank: device name updated; removed tank: its entities are deleted
            self.core.nodes.update(NID, {"name": "Jímka"})
            self.core.invalidate()
            self.assertTrue(await until(lambda: json.loads(b.retained[cfg_t])["device"]["name"] == "Jímka"))
            self.core.nodes.remove(NID)
            self.core.invalidate()
            self.assertTrue(await until(lambda: not any(obj in t or "/5555/" in t for t in b.retained)))
            self.assertEqual(json.load(open(os.path.join(self.tmp.name, mqtt.RETAINED_FILE))), [])
        self.run_with_broker(body)

    def test_switch_off_removes_everything(self):
        async def body(b, bridge):
            self.assertTrue(await until(lambda: len(b.retained) > 10))
            self.core.settings.update({"mqtt_host": ""})
            bridge.reconfigure()
            self.assertTrue(await until(lambda: not b.retained))
            self.assertTrue(await until(lambda: not bridge.status()["configured"]))
            self.assertFalse(bridge.connected)
        self.run_with_broker(body)

    def test_removed_while_off_is_cleaned_up(self):
        stale = "homeassistant/sensor/retencka_x_1234/hladina/config"
        with open(os.path.join(self.tmp.name, mqtt.RETAINED_FILE), "w") as f:
            json.dump([stale], f)

        async def body(b, bridge):
            b.retained[stale] = b"{}"
            self.assertTrue(await until(lambda: stale not in b.retained and len(b.retained) > 10))
        self.run_with_broker(body)

    def test_wrong_password(self):
        async def body(b, bridge):
            self.assertTrue(await until(lambda: bridge.status()["error"] == "Špatné jméno nebo heslo"))
            self.assertFalse(bridge.status()["connected"])
            self.assertEqual(b.retained, {})
        self.run_with_broker(body, password="jine")

    def test_tls_against_plain_broker(self):
        async def body(b, bridge):
            self.core.settings.update({"mqtt_tls": True})
            bridge.reconfigure()
            self.assertTrue(await until(lambda: (bridge.status()["error"] or "").startswith("Šifrované spojení")))
            self.assertFalse(bridge.status()["connected"])
        self.run_with_broker(body)

    def test_unreachable_broker(self):
        async def run():
            self.core.loop = asyncio.get_running_loop()
            self.core.settings.update({"mqtt_host": "127.0.0.1", "mqtt_port": 1})
            bridge = mqtt.MqttBridge(self.core)
            bridge.start(self.core.loop)
            self.assertTrue(await until(lambda: bridge.status()["error"] is not None))
            self.assertFalse(bridge.status()["connected"])
            await bridge.stop()
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()

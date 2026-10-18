"""Rain-aware automatic refill: wait for forecast rain, give up when it does not come."""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from retencka import model  # noqa: E402
from retencka.core import Core  # noqa: E402
from retencka.db import Database  # noqa: E402
from retencka.external import Weather  # noqa: E402
from retencka.settings import Settings  # noqa: E402


class RainWaitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db = Database(os.path.join(self.tmp.name, "t.db"))
        self.core = Core(db, Settings(db), self.tmp.name)
        self.core.nodes.add(0x1111, "T", {"tank_area_m2": 10.0, "max_depth_cm": 100.0, "catchment_area_m2": 100.0,
                                          "runoff_coef": 1.0, "refill_threshold_pct": 20.0})
        self.node = self.core.nodes.get(0x1111)

    def tearDown(self):
        self.tmp.cleanup()

    def lv(self, pct):
        return model.level_from_mm(pct * 10.0, self.node)          # 100 cm deep -> 1 % = 1 cm

    def rain(self, mm_per_hour, hours):
        now = int(time.time())
        self.core.weather._rain = [(now + h * 3600, mm_per_hour) for h in range(hours)]

    def test_gain(self):
        self.rain(1.0, 10)                                          # 10 mm × 100 m² = 1000 L = 10 % of 10 m³
        gain, mm, _ = self.core.rain_gain_pct(self.node, self.lv(15))
        self.assertAlmostEqual(gain, 10.0, delta=1.1)
        self.assertGreaterEqual(mm, 9.0)

    def test_wait_then_give_up(self):
        d = self.core._rain_wait_decision
        self.assertIsNone(d(0x1111, self.node, self.lv(18), 10.0, 10.0, None))          # waits
        self.assertIn("Čeká na déšť", self.core.refill_notes[0x1111])
        self.assertIsNone(d(0x1111, self.node, self.lv(15), 10.0, 10.0, None))          # -3 % still waits
        self.assertIn("klesla", d(0x1111, self.node, self.lv(12.5), 10.0, 10.0, None))  # -5.5 % -> refill

    def test_too_far_below(self):
        self.assertIsNotNone(self.core._rain_wait_decision(0x1111, self.node, self.lv(9), 50.0, 30.0, None))

    def test_not_enough_rain(self):
        self.assertEqual(self.core._rain_wait_decision(0x1111, self.node, self.lv(15), 3.0, 3.0, None), "")

    def test_deadline(self):
        d = self.core._rain_wait_decision
        self.assertIsNone(d(0x1111, self.node, self.lv(15), 10.0, 10.0, None))
        self.core._rain_wait[0x1111]["deadline"] = time.time() - 1
        self.assertIn("nepřišel", d(0x1111, self.node, self.lv(15), 10.0, 10.0, None))


class RefillLockTest(unittest.TestCase):
    """Two starts at the same time (manual + automatic) must not both open the valve."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        db = Database(os.path.join(self.tmp.name, "t.db"))
        self.core = Core(db, Settings(db), self.tmp.name)
        self.core.settings.update({"hydrawise_api_key": "key"})
        self.core.nodes.add(0x2222, "T", {"tank_area_m2": 10.0, "max_depth_cm": 100.0,
                                          "refill_provider": "hydrawise", "hydrawise_relay_id": "7"})
        self.core.last[0x2222] = {"ts": int(time.time()), "level_mm": 200, "flags": 0}
        self.calls = []

        async def fake_call(key, relay, action, seconds=0):
            self.calls.append(action)
            await asyncio.sleep(0.05)                        # slow cloud API
            return True, "OK"
        self.core.hydrawise.call = fake_call

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_start(self):
        async def run():
            return await asyncio.gather(self.core.refill_start(0x2222, 50, "manual", "a"),
                                        self.core.refill_start(0x2222, 50, "auto", "system"))
        results = asyncio.run(run())
        self.assertEqual(sorted(ok for ok, _ in results), [False, True])
        self.assertEqual(self.calls, ["run"])
        self.assertEqual(self.core.db.scalar("SELECT COUNT(*) FROM refills"), 1)


class DiskGuardTest(unittest.TestCase):
    def test_keeps_last_half_year(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Database(os.path.join(tmp, "t.db"))
            core = Core(db, Settings(db), tmp)
            core._free_mb = lambda: 10.0
            now = int(time.time())
            for i, age in enumerate((200, 185, 170)):
                db.execute("INSERT INTO measurements(ts, node_id, seq, flags) VALUES(?,?,?,0)",
                           (now - age * 86400, 1, i))
            core._disk_guard()
            left = [r[0] for r in db.query("SELECT seq FROM measurements ORDER BY seq")]
            self.assertEqual(left, [2])                      # 200 and 185 days gone, 170 days kept


class WeatherTest(unittest.TestCase):
    """Times from Open-Meteo must not depend on the time zone of the Pi."""

    def test_unixtime_parsing(self):
        now = int(time.time())
        hour = now - now % 3600
        offset = 7200                                        # location in UTC+2
        midnight = (now + offset) // 86400 * 86400 - offset  # local midnight as epoch
        body = {"utc_offset_seconds": offset,
                "daily": {"time": [midnight + i * 86400 for i in range(-1, 7)],
                          "precipitation_sum": [0.0] * 8, "temperature_2m_max": [20] * 8,
                          "temperature_2m_min": [10] * 8},
                "hourly": {"time": [hour - 3600, hour, hour + 3600, hour + 7200],
                           "precipitation": [5.0, 1.0, 2.0, 3.0]}}

        class FakeHttp:
            async def get(self, url, params):
                assert params["timeformat"] == "unixtime"
                return 200, json.dumps(body)

        w = Weather(FakeHttp())
        d = asyncio.run(w.forecast(50.0, 14.0))
        from datetime import datetime, timezone
        today = datetime.fromtimestamp(now + offset, timezone.utc).date().isoformat()
        self.assertEqual(d["time"][0], today)
        self.assertEqual(len(d["time"]), 7)
        mm, until = w.rain_ahead(2)                          # rain already fallen is not counted
        self.assertEqual(mm, 5.0)                            # hours ending at +1 h and +2 h
        self.assertEqual(until, hour + 7200)


class BatteryOffTest(unittest.TestCase):
    """Below 3.0 V the node sends a last packet and switches off – shown as such."""

    def test_off_after_last_packet(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Database(os.path.join(tmp, "t.db"))
            core = Core(db, Settings(db), tmp)
            core.nodes.add(0x3333, "T")
            now = int(time.time())
            core.last[0x3333] = {"ts": now, "vbat_mv": 2950, "flags": model.FLAG_BAT_LOW}
            self.assertFalse(core.battery(0x3333)["off"])        # still reporting
            core.last[0x3333]["ts"] = now - 3600
            b = core.battery(0x3333)
            self.assertTrue(b["off"] and b["low"])
            self.assertEqual(b["days_left"], 0)
            core.last[0x3333]["vbat_mv"] = 3400                  # just offline, battery fine
            self.assertFalse(core.battery(0x3333)["off"])


if __name__ == "__main__":
    unittest.main()

"""Rain-aware automatic refill: wait for forecast rain, give up when it does not come."""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from retencka import model  # noqa: E402
from retencka.core import Core  # noqa: E402
from retencka.db import Database  # noqa: E402
from retencka.settings import Settings  # noqa: E402


class RainWaitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
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


if __name__ == "__main__":
    unittest.main()
